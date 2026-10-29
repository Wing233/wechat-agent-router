"""Site report rendering and chunked delivery, owned by the site plugin."""
from __future__ import annotations

from datetime import datetime, timedelta

from data import CST


def _truncate(value: str, n: int) -> str:
    value = (value or "").replace("\n", " ").strip()
    return value[:n] + "…" if len(value) > n else value


def build_site_report(site, window_minutes: int = 30) -> str | None:
    now = datetime.now(CST)
    start = now - timedelta(minutes=max(1, window_minutes))
    start_iso, end_iso = start.isoformat(), now.isoformat()
    visits = site.visit_stats("custom", "none", start=start_iso, end=end_iso)
    visitors = site.visitor_detail("custom", 6, start=start_iso, end=end_iso)
    chats = site.chat_messages("custom", "", 8, start=start_iso, end=end_iso)
    totals = visits.get("totals") or {}
    if int(totals.get("pv") or 0) == 0 and int(chats.get("count") or 0) == 0:
        return None

    device_map = {"desktop": "桌面", "mobile": "手机", "tablet": "平板", "unknown": "未知"}
    source_map = {"direct": "直接", "search": "搜索", "social": "社交", "other": "其他"}
    lines = [f"📊 站点快报 · {start.strftime('%m-%d %H:%M')}–{now.strftime('%H:%M')}"]
    lines.append(f"👣 访问 {totals.get('pv', 0)} PV / {totals.get('uv', 0)} 位访客")
    for item in visitors.get("visitors", [])[:6]:
        head = (f"• 访客#{item['visitor']}（{device_map.get(item['device'], item['device'])}"
                f"·{source_map.get(item['source'], item['source'])}）{item['pv']} PV")
        lines.append(head)
        if item.get("ip"):
            lines.append(f"  IP {item['ip']} · {item.get('geo', '未知')}")
        if item.get("pages"):
            lines.append("  " + " → ".join(item["pages"][:3]))
    extra = int(visitors.get("total_visitors") or 0) - len(visitors.get("visitors", [])[:6])
    if extra > 0:
        lines.append(f"  另有 {extra} 位访客")
    if int(chats.get("count") or 0) > 0:
        lines.append(f"\n💬 AI 对话 {chats['count']} 条")
        for item in chats.get("items", [])[:8]:
            lines.append(f"• 访客#{item['visitor']} {item['time']}")
            lines.append(f"  问：{_truncate(item.get('message', ''), 80)}")
            lines.append(f"  答：{_truncate(item.get('answer', ''), 60)}")
    return "\n".join(lines)


def send_long(text: str, send_text) -> None:
    chunks, current = [], ""
    for line in text.split("\n"):
        if len(current) + len(line) + 1 > 1200 and current:
            chunks.append(current)
            current = line
        else:
            current = f"{current}\n{line}" if current else line
    if current:
        chunks.append(current)
    for index, chunk in enumerate(chunks, 1):
        prefix = f"({index}/{len(chunks)})\n" if len(chunks) > 1 else ""
        send_text(prefix + chunk)
