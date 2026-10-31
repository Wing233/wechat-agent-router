"""Minimal SSO endpoints backed by the existing Koishi Authenticator secret.

/sso/check is the Nginx ``auth_request`` target; /sso/login, /sso/setup and
/sso/logout are the browser-facing pages. The legacy /koishi-auth/* routes stay
untouched so the Koishi console can fall back to the old gate.

Integration (app.py):
    import sso_routes                     # with the other local imports
    sso_routes.register(app, CFG, SETTINGS, _koishi_page)   # last line
"""
from __future__ import annotations

import hmac
import html
import time
from urllib.parse import parse_qs

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse

import koishi_auth as ka
import sso_auth as sa

FAILS: dict[str, list[float]] = {}
MAX_FAILS = 12
FAIL_WINDOW = 300.0

STYLE = """
*{box-sizing:border-box}
body{margin:0;min-height:100vh;display:grid;place-items:center;background:#f3f5f7;
font-family:"Segoe UI","Microsoft YaHei UI",sans-serif;color:#111a22}
.card{width:min(440px,calc(100vw - 40px));background:#fff;border:1px solid #e3e8ec;
border-radius:18px;padding:30px 30px 26px;box-shadow:0 18px 48px rgba(16,24,40,.08)}
.eyebrow{font-size:10px;letter-spacing:.18em;color:#8b969f;font-weight:600}
h1{font-size:23px;margin:8px 0 6px;letter-spacing:-.02em}
p{font-size:13px;line-height:1.6;color:#68757f;margin:8px 0}
.secret{font-family:Consolas,monospace;font-size:19px;letter-spacing:.12em;text-align:center;
background:#e5f4f1;border:1px dashed #9fd4cc;color:#0a5b55;border-radius:12px;
padding:14px 10px;margin:14px 0;word-break:break-all}
code{display:block;background:#f8fafb;border:1px solid #e3e8ec;border-radius:9px;
padding:9px 10px;font-size:11px;word-break:break-all;color:#2b3945;margin:6px 0 14px}
label{display:block;font-size:12px;font-weight:600;margin:14px 0 6px}
input{width:100%;border:1px solid #d3dbe2;border-radius:10px;padding:11px 12px;
font-size:22px;letter-spacing:.36em;text-align:center;font-family:Consolas,monospace;outline:none}
input:focus{border-color:#6fbdb3;box-shadow:0 0 0 3px rgba(15,118,110,.16)}
button{width:100%;margin-top:16px;border:0;border-radius:10px;padding:11px 14px;
background:#0f766e;color:#fff;font-size:13px;font-weight:650;cursor:pointer}
button:hover{background:#0a5b55}
.err{background:#fdecea;border:1px solid #f5c9c5;color:#c2413a;border-radius:9px;
padding:9px 11px;font-size:12px;margin-top:14px}
.link{display:block;text-align:center;margin-top:14px;color:#0f766e;font-size:12px;text-decoration:none}
"""

PAGE = """<!doctype html>
<html lang="zh"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{{title}}</title><style>{{style}}</style></head>
<body><main class="card">{{body}}</main></body></html>"""


def _gate(cfg: dict, request: Request) -> None:
    expected = str((cfg.get("koishi_auth") or {}).get("gate_token") or "")
    supplied = request.headers.get("x-koishi-auth-gate", "")
    if not expected or not hmac.compare_digest(supplied.encode("utf-8"),
                                                expected.encode("utf-8")):
        raise HTTPException(404, "Not Found")


def _page(title: str, body: str, factory) -> HTMLResponse:
    if factory is not None:
        return factory(title, body)
    page_body = (PAGE.replace("{{title}}", html.escape(title))
                 .replace("{{style}}", STYLE).replace("{{body}}", body))
    return HTMLResponse(page_body, headers={"Cache-Control": "no-store",
                                            "X-Frame-Options": "DENY",
                                            "Referrer-Policy": "no-referrer"})


def _form(raw: bytes) -> dict:
    text = raw.decode("utf-8", "replace")
    return {key: (values[0] if values else "") for key, values in parse_qs(text).items()}


def _client_ip(request: Request) -> str:
    return request.headers.get("x-real-ip") or (request.client.host if request.client else "")


def _rate_ok(ip: str) -> bool:
    now = time.time()
    rows = [stamp for stamp in FAILS.get(ip, []) if now - stamp < FAIL_WINDOW]
    FAILS[ip] = rows
    return len(rows) < MAX_FAILS


def _fail(ip: str) -> None:
    FAILS.setdefault(ip, []).append(time.time())


def _login_body(next_url: str, error: str = "") -> str:
    return f"""
<div class="eyebrow">LYPH CONSOLE</div>
<h1>统一登录</h1>
<p>一次验证，进入 lyph 工作台与 Koishi 控制台。请输入 Authenticator 中的 6 位动态验证码。</p>
<form method="post" action="/sso/login?next={html.escape(next_url)}">
  <input type="hidden" name="csrf" value="__CSRF__">
  <label>动态验证码</label>
  <input name="code" inputmode="numeric" autocomplete="one-time-code" maxlength="6" required autofocus>
  <button type="submit">验证并进入</button>
</form>
{('<div class="err">' + html.escape(error) + '</div>') if error else ''}
<a class="link" href="/sso/logout">清除当前会话</a>
"""


def _setup_body(cfg: dict, error: str = "") -> str:
    auth = cfg.get("koishi_auth") or {}
    secret = str(auth.get("totp_secret") or "")
    username = str(auth.get("username") or "owner")
    pretty = " ".join(secret[i:i + 4] for i in range(0, len(secret), 4))
    uri = f"otpauth://totp/lyph:{username}?secret={secret}&issuer=lyph"
    return f"""
<div class="eyebrow">LYPH CONSOLE</div>
<h1>绑定 Authenticator</h1>
<p>这个密钥同时保护 lyph 工作台与 Koishi 控制台。用 Google Authenticator、Microsoft Authenticator 或 Authy 输入下面的密钥。</p>
<div class="secret">{html.escape(pretty)}</div>
<p>也可以复制这个 otpauth URI：</p>
<code>{html.escape(uri)}</code>
<form method="post" action="/sso/setup">
  <input type="hidden" name="csrf" value="__CSRF__">
  <label>输入 Authenticator 当前验证码</label>
  <input name="code" inputmode="numeric" autocomplete="one-time-code" maxlength="6" required>
  <button type="submit">验证并启用</button>
</form>
{('<div class="err">' + html.escape(error) + '</div>') if error else ''}
"""


def register(app: FastAPI, cfg: dict, settings, page_factory=None) -> None:
    """Mount /sso/*; safe to call after every other route is registered."""

    def page(title: str, body: str) -> HTMLResponse:
        body = body.replace("__CSRF__", html.escape(sa.form_csrf(cfg)))
        return _page(title, body, page_factory)

    @app.get("/sso/check")
    def sso_check(request: Request):
        _gate(cfg, request)
        value = sa.session_value(cfg, request)
        if value and sa.session_ok(cfg, request):
            header = sa.edge_header(cfg, value)
            if header:
                return Response(status_code=204, headers={
                    "Cache-Control": "no-store", "X-SSO-Verified": header})
        return Response(status_code=401, headers={"Cache-Control": "no-store"})

    @app.get("/sso/setup", response_class=HTMLResponse)
    def sso_setup(request: Request):
        _gate(cfg, request)
        auth = cfg.get("koishi_auth") or {}
        if auth.get("enrolled"):
            return RedirectResponse("/sso/login", status_code=303)
        return page("绑定 Authenticator", _setup_body(cfg))

    @app.post("/sso/setup", response_class=HTMLResponse)
    async def sso_setup_submit(request: Request):
        _gate(cfg, request)
        auth = cfg.get("koishi_auth") or {}
        if auth.get("enrolled"):
            return RedirectResponse("/sso/login", status_code=303)
        form = _form(await request.body())
        ip = _client_ip(request)
        error = ""
        if not hmac.compare_digest(str(form.get("csrf") or "").encode("utf-8"),
                                   sa.form_csrf(cfg).encode("utf-8")):
            error = "表单已过期，请刷新页面。"
        elif not _rate_ok(ip):
            error = "尝试次数过多，请 5 分钟后再试。"
        elif not ka.verify_totp(str(auth.get("totp_secret") or ""), str(form.get("code") or "")):
            _fail(ip)
            error = "验证码不对，请检查 Authenticator 的时间。"
        if error:
            return page("绑定 Authenticator", _setup_body(cfg, error))
        settings.enroll_koishi_auth()
        FAILS.pop(ip, None)
        response = RedirectResponse("/sso/login", status_code=303)
        sa.set_cookie(cfg, response)
        return response

    @app.get("/sso/login", response_class=HTMLResponse)
    def sso_login(request: Request):
        _gate(cfg, request)
        auth = cfg.get("koishi_auth") or {}
        if not auth.get("enrolled"):
            return RedirectResponse("/sso/setup", status_code=303)
        next_url = sa.safe_next(request.query_params.get("next", sa.DEFAULT_NEXT))
        if sa.session_ok(cfg, request):
            return RedirectResponse(next_url, status_code=303)
        return page("统一登录", _login_body(next_url))

    @app.post("/sso/login", response_class=HTMLResponse)
    async def sso_login_submit(request: Request):
        _gate(cfg, request)
        auth = cfg.get("koishi_auth") or {}
        if not auth.get("enrolled"):
            return RedirectResponse("/sso/setup", status_code=303)
        next_url = sa.safe_next(request.query_params.get("next", sa.DEFAULT_NEXT))
        form = _form(await request.body())
        ip = _client_ip(request)
        error = ""
        if not hmac.compare_digest(str(form.get("csrf") or "").encode("utf-8"),
                                   sa.form_csrf(cfg).encode("utf-8")):
            error = "表单已过期，请刷新页面。"
        elif not _rate_ok(ip):
            error = "尝试次数过多，请 5 分钟后再试。"
        elif not sa.verify_totp_once(str(auth.get("totp_secret") or ""),
                                     str(form.get("code") or "")):
            _fail(ip)
            error = "验证码不对或已使用，请稍后重试。"
        if error:
            return page("统一登录", _login_body(next_url, error))
        FAILS.pop(ip, None)
        response = RedirectResponse(next_url, status_code=303)
        sa.set_cookie(cfg, response)
        return response

    @app.get("/sso/logout")
    def sso_logout(request: Request):
        _gate(cfg, request)
        target = sa.safe_next(request.query_params.get("next", ""), fallback="/sso/login")
        response = RedirectResponse(target, status_code=303)
        sa.clear_cookies(cfg, response)
        return response
