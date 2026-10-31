"""Web search, page fetch, weather and calculator tools.

Search providers:
- bing: no API key, parses Bing HTML (default fallback)
- doubao: Volcengine Doubao Search API, requires a valid API key

The plugin deliberately keeps the search provider behind one tool so the LLM
doesn't need to know which backend is configured.
"""
from __future__ import annotations

import ast
import html
import ipaddress
import json
import math
import operator
import re
import socket
import ssl
import urllib.parse
import urllib.request
from html.parser import HTMLParser

BING_URL = "https://www.bing.com/search"
DOUBAO_URL = "https://open.feedcoopapi.com/search_api/web_search"
WEATHER_URL = "https://wttr.in/{city}?format=j1&lang=zh"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
MAX_FETCH_BYTES = 2 * 1024 * 1024


def _schema(properties: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": properties, "required": required or []}


def _text(value: str) -> str:
    value = re.sub(r"<[^>]+>", " ", value or "")
    return re.sub(r"\s+", " ", html.unescape(value)).strip()


class _PageText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.parts: list[str] = []
        self._in_title = False
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag == "title":
            self._in_title = True
        elif tag in ("script", "style", "noscript", "svg", "template"):
            self._skip += 1
        elif tag in ("p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "section", "article"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag == "title":
            self._in_title = False
        elif tag in ("script", "style", "noscript", "svg", "template") and self._skip:
            self._skip -= 1
        elif tag in ("p", "div", "li", "tr", "h1", "h2", "h3", "h4", "section", "article"):
            self.parts.append("\n")

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif not self._skip:
            self.parts.append(data)

    def result(self) -> str:
        text = "".join(self.parts)
        text = re.sub(r"[ \t\r\f\v]+", " ", text)
        text = re.sub(r"\n\s*\n+", "\n\n", text)
        return text.strip()


def _assert_public_url(url: str) -> None:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("只允许 http/https URL")
    host = parsed.hostname or ""
    if not host:
        raise ValueError("URL 缺少主机名")
    if host in ("localhost",) or host.endswith(".local"):
        raise ValueError("不允许访问本机或内网地址")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    if port not in (80, 443):
        raise ValueError("只允许 80/443 端口")
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ValueError(f"域名解析失败：{exc}") from None
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            raise ValueError("不允许访问内网、回环或保留地址")


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _assert_public_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _http(url: str, data: bytes | None = None, headers: dict | None = None,
          timeout: int = 25, max_bytes: int = MAX_FETCH_BYTES) -> tuple[str, str, str]:
    _assert_public_url(url)
    request = urllib.request.Request(url, data=data, method="POST" if data is not None else "GET")
    request.add_header("User-Agent", UA)
    request.add_header("Accept-Language", "zh-CN,zh;q=0.9,en;q=0.8")
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    opener = urllib.request.build_opener(
        _SafeRedirect(),
        urllib.request.HTTPSHandler(context=ssl.create_default_context()),
    )
    with opener.open(request, timeout=timeout) as response:
        content_type = response.headers.get("Content-Type", "")
        raw = response.read(max_bytes + 1)[:max_bytes]
        return response.geturl(), content_type, raw.decode("utf-8", "replace")


def _bing_search(query: str, count: int, time_range: str = "") -> list[dict]:
    params = {"q": query, "setlang": "zh-CN", "cc": "CN", "count": str(count)}
    window = {"OneDay": "ez1", "OneWeek": "ez2", "OneMonth": "ez3", "OneYear": "ez4"}.get(time_range)
    if window:
        params["filters"] = f'ex1:"{window}"'
    url = BING_URL + "?" + urllib.parse.urlencode(params)
    _, _, page = _http(url, timeout=20)
    results: list[dict] = []
    seen: set[str] = set()
    for block in re.finditer(r'<li class="b_algo".*?</li>', page, re.S):
        chunk = block.group(0)
        match = re.search(r'<h2[^>]*>.*?<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', chunk, re.S)
        if not match:
            continue
        link = html.unescape(match.group(1))
        if link.startswith("/"):
            link = "https://www.bing.com" + link
        if not link.startswith(("http://", "https://")) or link in seen:
            continue
        snippet_match = re.search(r"<p[^>]*>(.*?)</p>", chunk, re.S)
        seen.add(link)
        results.append({
            "title": _text(match.group(2))[:180],
            "url": link,
            "site": urllib.parse.urlsplit(link).hostname or "",
            "snippet": _text(snippet_match.group(1))[:600] if snippet_match else "",
        })
        if len(results) >= count:
            break
    return results


def _doubao_search(query: str, count: int, api_key: str, time_range: str = "") -> list[dict]:
    body = {"Query": query, "SearchType": "web", "Count": max(1, min(count, 10)),
            "NeedSummary": True}
    if time_range in ("OneDay", "OneWeek", "OneMonth", "OneYear"):
        body["TimeRange"] = time_range
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    _, _, raw = _http(DOUBAO_URL, data=data, timeout=30, headers={
        "Content-Type": "application/json",
        "X-Traffic-Tag": "skill_web_search_common",
        "Authorization": "Bearer " + api_key,
    })
    payload = json.loads(raw)
    error = (payload.get("ResponseMetadata") or {}).get("Error")
    if error:
        raise RuntimeError(f"豆包搜索失败：{error.get('Message') or error.get('Code')}")
    rows = []
    for item in ((payload.get("Result") or {}).get("WebResults") or [])[:count]:
        rows.append({
            "title": str(item.get("Title") or "")[:180],
            "url": str(item.get("Url") or ""),
            "site": str(item.get("SiteName") or ""),
            "snippet": str(item.get("Summary") or item.get("Snippet") or "")[:600],
        })
    return rows


def apply(ctx) -> None:
    def _settings() -> dict:
        return ctx.root_config.get("websearch") or {}

    def _key() -> str:
        try:
            return ctx.secret("web_search_api_key")
        except Exception:
            return ""

    def web_search(args: dict) -> dict:
        query = str(args.get("query") or "").strip()[:200]
        if not query:
            return {"error": "搜索词不能为空"}
        settings = _settings()
        provider = str(settings.get("provider") or "bing").lower()
        default_count = max(1, min(int(settings.get("default_count") or 5), 10))
        count = max(1, min(int(args.get("count") or default_count), 10))
        time_range = str(args.get("time_range") or "")
        warning = ""
        rows: list[dict] = []
        if provider == "doubao":
            key = _key()
            if not key:
                warning = "未配置豆包搜索 Key，已改用 Bing"
            else:
                try:
                    rows = _doubao_search(query, count, key, time_range)
                except Exception as exc:
                    warning = f"豆包搜索失败，已改用 Bing：{exc}"
        if not rows:
            try:
                rows = _bing_search(query, count, time_range)
            except Exception as exc:
                return {"error": f"搜索失败：{exc}"}
        return {"provider": provider if not warning else "bing", "query": query,
                "warning": warning, "results": rows}

    def web_fetch(args: dict) -> dict:
        url = str(args.get("url") or "").strip()
        if not url:
            return {"error": "URL 不能为空"}
        max_chars = max(1000, min(int(args.get("max_chars") or 6000), 12000))
        try:
            final_url, content_type, raw = _http(url, timeout=25)
        except Exception as exc:
            return {"error": f"抓取失败：{exc}"}
        if "html" in content_type.lower():
            parser = _PageText()
            try:
                parser.feed(raw)
            except Exception:
                pass
            text = parser.result()
            title = _text(parser.title)[:200]
        else:
            text = re.sub(r"\n{3,}", "\n\n", raw)
            title = ""
        if not text:
            return {"error": "页面没有可提取的文本", "url": final_url}
        return {"url": final_url, "title": title, "content_type": content_type,
                "text": text[:max_chars], "truncated": len(text) > max_chars}

    def weather(args: dict) -> dict:
        city = str(args.get("city") or "北京").strip()[:60]
        url = WEATHER_URL.format(city=urllib.parse.quote(city))
        try:
            _, _, raw = _http(url, timeout=20)
            data = json.loads(raw)
            current = (data.get("current_condition") or [{}])[0]
            area = ((data.get("nearest_area") or [{}])[0])
            name = ((area.get("areaName") or [{}])[0].get("value") or city)
            country = ((area.get("country") or [{}])[0].get("value") or "")
            desc = ((current.get("weatherDesc") or [{}])[0].get("value") or "")
            return {
                "city": name, "country": country,
                "temperature_c": current.get("temp_C"),
                "feels_like_c": current.get("FeelsLikeC"),
                "description": desc,
                "humidity": current.get("humidity"),
                "wind": f"{current.get('winddir16Point', '')} {current.get('windspeedKmph', '')} km/h",
                "observation_time": current.get("localObsDateTime") or current.get("observation_time"),
            }
        except Exception as exc:
            return {"error": f"天气查询失败：{exc}"}

    _BIN = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
            ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv,
            ast.Mod: operator.mod, ast.Pow: operator.pow}
    _FUNCS = {"sqrt": math.sqrt, "sin": math.sin, "cos": math.cos, "tan": math.tan,
              "log": math.log, "log10": math.log10, "exp": math.exp, "abs": abs,
              "round": round, "min": min, "max": max}

    def _calc(node):
        if isinstance(node, ast.Expression):
            return _calc(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return node.value
        if isinstance(node, ast.BinOp) and type(node.op) in _BIN:
            left, right = _calc(node.left), _calc(node.right)
            if isinstance(node.op, ast.Pow) and abs(right) > 10000:
                raise ValueError("指数过大")
            return _BIN[type(node.op)](left, right)
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            value = _calc(node.operand)
            return value if isinstance(node.op, ast.UAdd) else -value
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _FUNCS:
            return _FUNCS[node.func.id](*[_calc(arg) for arg in node.args])
        if isinstance(node, ast.Name) and node.id in ("pi", "e"):
            return math.pi if node.id == "pi" else math.e
        raise ValueError("表达式包含不支持的语法")

    def calculator(args: dict) -> dict:
        expression = str(args.get("expression") or "").strip()[:200]
        if not expression:
            return {"error": "表达式不能为空"}
        try:
            result = _calc(ast.parse(expression, mode="eval"))
        except Exception as exc:
            return {"error": f"计算失败：{exc}"}
        return {"expression": expression, "result": result}

    ctx.tools.register(
        "web_search",
        "联网搜索实时信息、新闻、价格、政策、人物、产品等。涉及事实或时效性内容时先搜索；返回标题、来源、摘要和 URL。",
        _schema({
            "query": {"type": "string", "description": "搜索词，1-200 字"},
            "count": {"type": "integer", "minimum": 1, "maximum": 10},
            "time_range": {"type": "string", "enum": ["", "OneDay", "OneWeek", "OneMonth", "OneYear"]},
        }, ["query"]),
        web_search, aliases=("web_search",),
    )
    ctx.tools.register(
        "web_fetch",
        "抓取一个公开网页的正文，返回标题和文本。用于读新闻、文档、公告的详细内容；不能访问本机或内网地址。",
        _schema({
            "url": {"type": "string", "description": "http/https 公开 URL"},
            "max_chars": {"type": "integer", "minimum": 1000, "maximum": 12000},
        }, ["url"]),
        web_fetch, aliases=("web_fetch", "fetch_url"),
    )
    ctx.tools.register(
        "weather",
        "查询城市当前天气和体感温度。",
        _schema({"city": {"type": "string", "description": "城市名，例如 北京、上海"}}, ["city"]),
        weather, aliases=("weather",),
    )
    ctx.tools.register(
        "calculator",
        "计算数学表达式；支持 + - * / // % **、sqrt/sin/cos/log 等函数和 pi/e。",
        _schema({"expression": {"type": "string", "description": "数学表达式"}}, ["expression"]),
        calculator, aliases=("calculator", "calc"),
    )
    ctx.log.info("websearch tools registered (provider=%s)", _settings().get("provider", "bing"))
