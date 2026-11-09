"""Optional SMTP fallback for notifications that must arrive outside WeChat's 24h window."""
from __future__ import annotations

import smtplib
import ssl
from email.message import EmailMessage
from typing import Any


def send_text(text: str, config: dict[str, Any] | None) -> tuple[bool, str]:
    cfg = config or {}
    if not cfg.get("enabled"):
        return False, "email fallback is disabled"
    host = str(cfg.get("host") or "").strip()
    sender = str(cfg.get("from") or "").strip()
    recipients = cfg.get("to") or []
    if isinstance(recipients, str):
        recipients = [item.strip() for item in recipients.split(",") if item.strip()]
    recipients = [str(item).strip() for item in recipients if str(item).strip()]
    if not host or not sender or not recipients:
        return False, "email host/from/to is incomplete"
    port = int(cfg.get("port") or 587)
    message = EmailMessage()
    message["Subject"] = str(cfg.get("subject") or "LYPH 通知")
    message["From"] = sender
    message["To"] = ", ".join(recipients)
    message.set_content(str(text)[:20000])
    try:
        if cfg.get("ssl"):
            with smtplib.SMTP_SSL(host, port, timeout=20,
                                  context=ssl.create_default_context()) as client:
                _login(client, cfg)
                client.send_message(message)
        else:
            with smtplib.SMTP(host, port, timeout=20) as client:
                if cfg.get("starttls", True):
                    client.starttls(context=ssl.create_default_context())
                _login(client, cfg)
                client.send_message(message)
    except Exception as exc:
        detail = str(exc)
        password = str(cfg.get("password") or "")
        if password:
            detail = detail.replace(password, "***")
        return False, detail[:300]
    return True, ""


def _login(client, cfg: dict[str, Any]) -> None:
    username = str(cfg.get("username") or "").strip()
    password = str(cfg.get("password") or "")
    if username:
        client.login(username, password)
