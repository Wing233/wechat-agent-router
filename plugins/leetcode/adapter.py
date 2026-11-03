"""LeetCode.cn adapter: cookie validation, stats, and re-login prompts."""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

from cryptography.fernet import Fernet, InvalidToken

GRAPHQL = "https://leetcode.cn/graphql/"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
CST = timezone(timedelta(hours=8))
SUBMISSION_PAGE_SIZE = 20
MAX_SUBMISSION_PAGES = 20

STATUS_QUERY = "query { userStatus { isSignedIn username } }"
STATS_QUERY = """
query($userSlug: String!) {
  userProfilePublicProfile(userSlug: $userSlug) {
    username
    siteRanking
    profile { realName userAvatar }
  }
  userProfileUserQuestionProgress(userSlug: $userSlug) {
    numAcceptedQuestions { difficulty count }
  }
  recentSubmitted(userSlug: $userSlug) {
    submitTime
    status
    lang
    question { title titleSlug }
  }
}
"""

QUESTION_SEARCH_QUERY = """
query problemsetQuestionList($categorySlug: String, $limit: Int, $skip: Int, $filters: QuestionListFilterInput) {
  problemsetQuestionList: questionList(
    categorySlug: $categorySlug
    limit: $limit
    skip: $skip
    filters: $filters
  ) {
    totalNum
    questions: data {
      frontendId
      title
      titleCn
      titleSlug
      difficulty
      status
    }
  }
}
"""

QUESTION_DETAIL_QUERY = """
query questionData($titleSlug: String!) {
  question(titleSlug: $titleSlug) {
    questionFrontendId
    title
    translatedTitle
    titleSlug
    difficulty
  }
}
"""

SUBMISSIONS_QUERY = """
query submissionList($offset: Int!, $limit: Int!, $lastKey: String, $questionSlug: String!) {
  submissionList(offset: $offset, limit: $limit, lastKey: $lastKey, questionSlug: $questionSlug) {
    lastKey
    hasNext
    submissions {
      id
      statusDisplay
      lang
      runtime
      timestamp
      url
      isPending
      memory
    }
  }
}
"""


class QuestionNotFound(Exception):
    pass


def _iso_time(value) -> str:
    try:
        return datetime.fromtimestamp(int(value), tz=CST).isoformat(timespec="seconds")
    except (TypeError, ValueError, OSError):
        return ""


def _date_key(value) -> str:
    try:
        return datetime.fromtimestamp(int(value), tz=CST).date().isoformat()
    except (TypeError, ValueError, OSError):
        return ""


def _timestamp(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _question_item(item: dict) -> dict:
    title_en = str(item.get("title") or "").strip()
    title_cn = str(item.get("titleCn") or item.get("translatedTitle") or "").strip()
    return {
        "frontend_id": str(item.get("frontendId") or item.get("questionFrontendId") or "").strip(),
        "title": title_cn or title_en,
        "title_en": title_en,
        "slug": str(item.get("titleSlug") or "").strip(),
        "difficulty": str(item.get("difficulty") or "").strip(),
        "status": str(item.get("status") or "").strip(),
    }


def _slug_from_input(value: str) -> str:
    match = re.search(r"/problems/([A-Za-z0-9_-]+)", value)
    if match:
        return match.group(1)
    if re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)+", value):
        return value
    return ""


LOGIN_PAGE = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>绑定力扣登录</title>
<style>
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;max-width:640px;margin:24px auto;padding:0 16px;color:#222;line-height:1.6}
h1{font-size:20px}label{display:block;margin:14px 0 6px;font-weight:600}
textarea,input{width:100%;padding:10px;border:1px solid #ccc;border-radius:8px;font-size:14px;box-sizing:border-box}
textarea{height:120px}button{margin-top:16px;width:100%;padding:12px;border:0;border-radius:8px;background:#111;color:#fff;font-size:16px}
.hint{background:#f6f6f6;padding:12px;border-radius:8px;font-size:13px;color:#555}
.err{color:#b00020}.ok{color:#0a7a2f}
</style></head><body>
<h1>绑定力扣（leetcode.cn）登录</h1>
<p class="hint">1. 电脑浏览器登录 leetcode.cn<br>
2. 打开开发者工具 → Application/应用 → Cookies → https://leetcode.cn<br>
3. 复制 <b>LEETCODE_SESSION</b> 的值填到下面第一栏；<b>csrftoken</b> 填到第二栏<br>
4. 提交后会立即校验并加密保存</p>
{message}
<form method="post">
  <label>LEETCODE_SESSION</label>
  <textarea name="session" required placeholder="粘贴 LEETCODE_SESSION 的值"></textarea>
  <label>csrftoken</label>
  <input name="csrf" required placeholder="粘贴 csrftoken 的值">
  <button type="submit">保存并校验</button>
</form>
</body></html>"""


def _parse_cookie(text: str) -> str:
    text = (text or "").strip()
    if not text:
        return ""
    if "=" in text and ";" in text:
        return re.sub(r"\s+", " ", text)
    return text


def cookie_token(cookie: str, name: str) -> str:
    match = re.search(rf"(?:^|;\s*){re.escape(name)}=([^;]+)", cookie or "")
    return match.group(1) if match else ""


class LeetCode:
    def __init__(self, store, fernet_key: str, notify, login_url):
        self.store = store
        self.fernet = Fernet(fernet_key.encode())
        self.notify = notify
        self.login_url = login_url
        self._last_status: float = 0

    # ------------------------------------------------------------------
    def _headers(self, cookie: str = "") -> dict:
        headers = {
            "Content-Type": "application/json",
            "Referer": "https://leetcode.cn/",
            "Origin": "https://leetcode.cn",
            "User-Agent": UA,
            "Accept": "application/json",
        }
        if cookie:
            headers["Cookie"] = cookie
            csrf = cookie_token(cookie, "csrftoken")
            if csrf:
                headers["x-csrftoken"] = csrf
        return headers

    def _graphql(self, query: str, variables: dict | None = None, cookie: str = "",
                 referer: str = "https://leetcode.cn/") -> dict:
        body = json.dumps({"query": query, "variables": variables or {}}).encode()
        headers = self._headers(cookie)
        headers["Referer"] = referer
        req = urllib.request.Request(GRAPHQL, data=body, method="POST", headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"leetcode HTTP {exc.code}") from None
        except Exception as exc:
            raise RuntimeError(f"leetcode request failed: {exc}") from None

    def validate(self, cookie: str) -> tuple[bool, str, str]:
        try:
            data = self._graphql(STATUS_QUERY, cookie=cookie)
        except Exception as exc:
            return False, "", str(exc)
        status = (data.get("data") or {}).get("userStatus") or {}
        if status.get("isSignedIn"):
            return True, status.get("username") or "", ""
        errors = data.get("errors") or []
        return False, "", (errors[0].get("message") if errors else "登录状态无效")

    def fetch(self, username: str, cookie: str = "") -> dict:
        data = self._graphql(STATS_QUERY, {"userSlug": username}, cookie=cookie)
        if data.get("errors"):
            raise RuntimeError(str(data["errors"])[:300])
        body = data.get("data") or {}
        profile = body.get("userProfilePublicProfile") or {}
        progress = body.get("userProfileUserQuestionProgress") or {}
        recent = body.get("recentSubmitted") or []
        counts = {"EASY": 0, "MEDIUM": 0, "HARD": 0}
        for item in progress.get("numAcceptedQuestions") or []:
            key = str(item.get("difficulty") or "").upper()
            if key in counts:
                counts[key] = int(item.get("count") or 0)
        return {
            "username": profile.get("username") or username,
            "real_name": (profile.get("profile") or {}).get("realName") or "",
            "ranking": profile.get("siteRanking"),
            "solved": {
                "easy": counts["EASY"],
                "medium": counts["MEDIUM"],
                "hard": counts["HARD"],
                "total": sum(counts.values()),
            },
            "recent": [
                {
                    "title": ((item.get("question") or {}).get("title") or ""),
                    "slug": ((item.get("question") or {}).get("titleSlug") or ""),
                    "status": item.get("status"),
                    "lang": item.get("lang"),
                    "time": item.get("submitTime"),
                }
                for item in recent
            ],
        }

    # ------------------------------------------------------------------
    def _load_cookie(self) -> tuple[str, str, str]:
        cred = self.store.get_credential("leetcode")
        if not cred:
            return "", "", "missing"
        blob = (cred.get("secret") or {}).get("blob", "")
        try:
            raw = self.fernet.decrypt(blob.encode()).decode()
        except (InvalidToken, ValueError, AttributeError):
            return "", cred.get("account", ""), "expired"
        secret = json.loads(raw)
        return secret.get("cookie", ""), cred.get("account", ""), cred.get("status", "missing")

    def _auth(self) -> tuple[str, str, str]:
        cookie, account, status = self._load_cookie()
        checked = time.time()
        if cookie and (status == "active" or checked - self._last_status > 300):
            ok, username, error = self.validate(cookie)
            self._last_status = checked
            if ok:
                account = username
                status = "active"
                self.store.mark_credential("leetcode", "active", "")
            else:
                self.store.mark_credential("leetcode", "expired", error)
                self.notify_login("expired")
                status = "expired"
        return cookie, account, status

    def _resolve_question(self, question: str) -> dict:
        question = (question or "").strip()
        if not question:
            raise QuestionNotFound()
        slug = _slug_from_input(question)
        if slug:
            try:
                data = self._graphql(QUESTION_DETAIL_QUERY, {"titleSlug": slug})
                node = (data.get("data") or {}).get("question") or {}
                if node:
                    return _question_item(node)
            except RuntimeError:
                pass
        data = self._graphql(QUESTION_SEARCH_QUERY, {
            "categorySlug": "",
            "limit": 20,
            "skip": 0,
            "filters": {"searchKeywords": question},
        })
        if data.get("errors"):
            raise RuntimeError(str(data["errors"])[:300])
        node = (data.get("data") or {}).get("problemsetQuestionList") or {}
        questions = [_question_item(item) for item in node.get("questions") or [] if isinstance(item, dict)]
        if not questions:
            raise QuestionNotFound()
        needle = question.lstrip("#").strip()
        if needle.isdigit():
            for item in questions:
                if item["frontend_id"].lstrip("0") == needle.lstrip("0"):
                    return item
        lowered = needle.casefold()
        for item in questions:
            if lowered in {item["title"].casefold(), item["title_en"].casefold()}:
                return item
        return questions[0]

    def _fetch_submissions(self, slug: str, cookie: str) -> tuple[list[dict], bool]:
        items: list[dict] = []
        last_key = ""
        offset = 0
        seen: set[tuple[str, ...]] = set()
        has_next = False
        for _ in range(MAX_SUBMISSION_PAGES):
            data = self._graphql(SUBMISSIONS_QUERY, {
                "offset": offset,
                "limit": SUBMISSION_PAGE_SIZE,
                "lastKey": last_key,
                "questionSlug": slug,
            }, cookie=cookie, referer="https://leetcode.cn/progress/")
            if data.get("errors"):
                raise RuntimeError(str(data["errors"])[:300])
            node = (data.get("data") or {}).get("submissionList")
            if node is None:
                raise RuntimeError("login_expired")
            page = [item for item in node.get("submissions") or [] if isinstance(item, dict)]
            signature = tuple(str(item.get("id") or "") for item in page)
            if signature and signature in seen:
                break
            if signature:
                seen.add(signature)
            items.extend(page)
            has_next = bool(node.get("hasNext"))
            if not has_next or not page:
                break
            last_key = str(node.get("lastKey") or "")
            offset += SUBMISSION_PAGE_SIZE
        return items, has_next

    def get_question_record(self, question: str, limit: int = 5) -> dict:
        checked = datetime.now(timezone.utc).isoformat(timespec="seconds")
        cookie, account, status = self._auth()
        if not cookie:
            self.notify_login("missing")
            return {"login_status": "missing", "error": "尚未绑定力扣登录信息", "checked_at": checked}
        if status != "active":
            return {"login_status": status, "error": "力扣登录已过期，需要重新登录",
                    "username": account, "checked_at": checked}
        try:
            info = self._resolve_question(question)
        except QuestionNotFound:
            return {"login_status": "ok", "error": "not_found", "message": "没有找到这道题",
                    "username": account, "checked_at": checked}
        except Exception as exc:
            return {"login_status": "error", "error": str(exc)[:300], "username": account, "checked_at": checked}
        try:
            submissions, truncated = self._fetch_submissions(info["slug"], cookie)
        except Exception as exc:
            message = str(exc)
            if "login_expired" in message:
                self.store.mark_credential("leetcode", "expired", message)
                self.notify_login("expired")
                return {"login_status": "expired", "error": "力扣登录已过期，需要重新登录",
                        "username": account, "checked_at": checked}
            return {"login_status": "error", "error": message[:300], "username": account, "checked_at": checked}
        timestamps = [_timestamp(item.get("timestamp")) for item in submissions]
        timestamps = [value for value in timestamps if value > 0]
        accepted = [_timestamp(item.get("timestamp")) for item in submissions
                    if str(item.get("statusDisplay") or "").casefold() == "accepted"]
        accepted = [value for value in accepted if value > 0]
        accepted_days = sorted({_date_key(value) for value in accepted if _date_key(value)})
        recent = sorted(submissions, key=lambda item: _timestamp(item.get("timestamp")), reverse=True)[:limit]
        return {
            "login_status": "ok",
            "username": account,
            "question": info,
            "attempted": bool(submissions),
            "first_submitted_at": _iso_time(min(timestamps)) if timestamps else None,
            "last_submitted_at": _iso_time(max(timestamps)) if timestamps else None,
            "first_accepted_at": _iso_time(min(accepted)) if accepted else None,
            "last_accepted_at": _iso_time(max(accepted)) if accepted else None,
            "submission_count": len(submissions),
            "accepted_count": len(accepted),
            "accepted_days": len(accepted_days),
            "redo_count": max(0, len(accepted_days) - 1),
            "truncated": truncated,
            "recent": [
                {
                    "time": _iso_time(item.get("timestamp")),
                    "status": str(item.get("statusDisplay") or ""),
                    "lang": str(item.get("lang") or ""),
                    "runtime": str(item.get("runtime") or ""),
                    "memory": str(item.get("memory") or ""),
                }
                for item in recent
            ],
            "checked_at": checked,
            "source_note": ("首次时间基于最近读取的提交记录，若记录较多可能不是真正首次；最近一次提交时间不受影响。"
                            if truncated else "首次和最近一次均来自已读取的全部提交记录。"),
        }

    def get_stats(self, limit: int = 10) -> dict:
        cookie, account, status = self._auth()
        if account:
            try:
                data = self.fetch(account, cookie if status == "active" else "")
                data["recent"] = data["recent"][:limit]
                data["login_status"] = "ok" if status == "active" else status
                data["checked_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
                return data
            except Exception as exc:
                return {"login_status": "error", "error": str(exc)[:300], "username": account}
        self.notify_login("missing")
        return {"login_status": "missing", "error": "尚未绑定力扣登录信息"}

    def save_cookie(self, account_hint: str, cookie: str) -> dict:
        cookie = _parse_cookie(cookie)
        if "LEETCODE_SESSION" not in cookie:
            return {"ok": False, "error": "Cookie 中缺少 LEETCODE_SESSION"}
        ok, username, error = self.validate(cookie)
        if not ok:
            return {"ok": False, "error": error or "Cookie 无效或已过期"}
        blob = self.fernet.encrypt(json.dumps({"cookie": cookie}).encode()).decode()
        self.store.save_credential("leetcode", username or account_hint, {"blob": blob}, "active")
        return {"ok": True, "username": username}

    def login_html(self, message: str = "") -> str:
        return LOGIN_PAGE.replace("{message}", message)

    def submit(self, form: dict) -> dict:
        session = (form.get("session") or "").strip()
        csrf = (form.get("csrf") or "").strip()
        if not session:
            return {"ok": False, "error": "LEETCODE_SESSION 不能为空"}
        cookie = session if "LEETCODE_SESSION=" in session else f"LEETCODE_SESSION={session}"
        if csrf:
            cookie += f"; csrftoken={csrf}"
        return self.save_cookie("", cookie)

    def notify_login(self, reason: str, force: bool = False) -> None:
        last = self.store.state_get("leetcode_notified_at", "")
        if last and not force:
            try:
                if (datetime.now(timezone.utc) - datetime.fromisoformat(last)).total_seconds() < 6 * 3600:
                    return
            except Exception:
                pass
        self.store.state_set("leetcode_notified_at", datetime.now(timezone.utc).isoformat(timespec="seconds"))
        url = self.login_url("leetcode")
        text = (
            "🔐 力扣登录已过期，需要更新 Cookie。\n"
            f"请在 30 分钟内打开：\n{url}\n"
            "打开后粘贴浏览器里的 LEETCODE_SESSION 和 csrftoken。\n"
            "（力扣不支持网页扫码登录，手机验证码带滑块验证，人工粘贴 Cookie 最稳）"
        ) if reason == "expired" else (
            "🔐 还没有绑定力扣账号。\n"
            f"请打开：\n{url}\n"
            "登录力扣后，从浏览器开发者工具复制 Cookie 粘贴进来。"
        )
        try:
            self.notify(text)
        except Exception:
            pass
