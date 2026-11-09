#!/usr/bin/env python3
"""Remove screenshot captures that block worklog daily publishing.

Why this exists
---------------
The Rust desktop MVP uploaded screenshots (`source_kind=screenshot`) to the
same LYPH device that the daily worklog note uses.  `worklog_publish.py` only
accepts days whose captures are all text `note`s, so those days can never be
published.

The current Rust desktop no longer uploads screenshots.  This script cleans up
the days that were polluted, so the normal publisher can run again.

Safety
------
- Read-only by default: pass `--apply` to actually delete.
- Only touches `source_kind='screenshot'` rows and their stored objects.
- Scope is limited to `--since` (default: 2026-11-08) through `--until`
  (default: today, Asia/Shanghai).

Two-phase on purpose: the capture rows and their file references are deleted
and committed first, then the storage objects are removed.  Deleting objects
while the capture rows still reference them is rejected by `storage.delete`
(409) and holding a write transaction across object deletion causes
"database is locked".

Run inside the LYPH API container:

    docker cp lyph_cleanup_screenshot_captures.py lyph-api-1:/tmp/
    docker exec lyph-api-1 python /tmp/lyph_cleanup_screenshot_captures.py --since 2026-11-08
    docker exec lyph-api-1 python /tmp/lyph_cleanup_screenshot_captures.py --since 2026-11-08 --apply

Then republish the affected days:

    docker exec lyph-api-1 python /data/worklog_publish.py --date 2026-11-09 --visibility public
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from contextlib import closing
from datetime import datetime
from zoneinfo import ZoneInfo

DB_PATH = "/data/lyph.sqlite3"


def _unique_object_ids(rows) -> list[str]:
    seen: list[str] = []
    for row in rows:
        object_id = row["object_id"]
        if object_id and object_id not in seen:
            seen.append(object_id)
    return seen


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=DB_PATH)
    parser.add_argument("--since", default="2026-11-08")
    parser.add_argument(
        "--until", default=datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
    )
    parser.add_argument("--apply", action="store_true", help="actually delete")
    args = parser.parse_args()

    conn = sqlite3.connect(args.db, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    rows = conn.execute(
        "SELECT id, captured_day, object_id FROM captures "
        "WHERE source_kind='screenshot' AND captured_day>=? AND captured_day<=? "
        "ORDER BY captured_day, id",
        (args.since, args.until),
    ).fetchall()
    by_day: dict[str, int] = {}
    for row in rows:
        by_day[row["captured_day"]] = by_day.get(row["captured_day"], 0) + 1
    if not rows:
        print("没有需要清理的截图采集。")
        return 0
    print("待清理的截图采集：")
    for day, count in sorted(by_day.items()):
        print(f"  {day}: {count} 条")
    print(f"合计 {len(rows)} 条（涉及 {len(_unique_object_ids(rows))} 个存储对象）。")

    if not args.apply:
        print("\n这是预演。确认后加 --apply 才会删除。")
        return 0

    capture_ids = [row["id"] for row in rows]
    object_ids = _unique_object_ids(rows)

    # Phase 1: drop capture rows and their file references, then commit.
    deleted = 0
    conn.execute("BEGIN IMMEDIATE")
    try:
        for start in range(0, len(capture_ids), 200):
            chunk = capture_ids[start:start + 200]
            marks = ",".join("?" for _ in chunk)
            conn.execute(
                f"DELETE FROM file_references WHERE owner_type='capture' AND owner_id IN ({marks})",
                chunk,
            )
            deleted += conn.execute(
                f"DELETE FROM captures WHERE id IN ({marks})", chunk
            ).rowcount
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    print(f"已删除 {deleted} 条截图采集和对应引用。")

    # Phase 2: storage objects are now unreferenced; delete them outside any transaction.
    sys.path.insert(0, "/app")
    try:
        from lyph import storage  # type: ignore
    except Exception as exc:  # pragma: no cover - only used on the server
        print(f"无法导入 lyph.storage：{exc}", file=sys.stderr)
        print("采集行已删除；存储对象保留，可稍后重跑本脚本清理。")
        return 2

    removed = missing = failed = 0
    for index, object_id in enumerate(object_ids, 1):
        try:
            storage.delete(object_id)
            removed += 1
        except Exception as exc:
            message = str(exc)
            if "File not found" in message or "404" in message:
                missing += 1
            else:
                failed += 1
                print(f"  ! 删除对象 {object_id} 失败：{message}", file=sys.stderr)
        if index % 50 == 0:
            print(f"  ... {index}/{len(object_ids)}")
    print(f"存储对象：已删除 {removed}，已不存在 {missing}，失败 {failed}。")
    if failed:
        print("可以先继续发布；剩余对象可稍后重跑本脚本清理。")
    print("下一步：对需要的日期执行 worklog_publish.py 重新发布。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
