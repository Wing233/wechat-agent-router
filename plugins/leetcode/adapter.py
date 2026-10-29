"""LeetCode.cn adapter: cookie validation, stats, and re-login prompts."""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

from cryptography.fernet import Fernet, InvalidToken

GRAPHQL = "https://leetcode.cn/graphql/"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"

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

    def _graphql(self, query: str, variables: dict | None = None, cookie: str = "") -> dict:
        body = json.dumps({"query": query, "variables": variables or {}}).encode()
        req = urllib.request.Request(GRAPHQL, data=body, method="POST", headers=self._headers(cookie))
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

    def get_stats(self, limit: int = 10) -> dict:
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
