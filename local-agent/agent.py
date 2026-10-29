"""CLI entry for the outbound-polling local agent.

For the desktop console run ``desktop/main.py`` instead; both share agent_core.py.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from agent_core import EventLog, LocalAgent, load_config, normalize_config, save_config


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=str(Path(__file__).with_name("config.json")))
    parser.add_argument("--check", action="store_true", help="校验配置后退出")
    args = parser.parse_args()
    config_path = Path(args.config)
    try:
        config = load_config(config_path)
        save_config(config_path, config)
    except Exception as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        raise SystemExit(2) from None
    if args.check:
        print(json.dumps(normalize_config(config), ensure_ascii=False, indent=2))
        return
    log = EventLog(file_path=config_path.with_name("agent.log"),
                   printer=lambda item: print(f"{item['ts']} [{item['level']}] {item['message']}",
                                              file=sys.stderr, flush=True))
    agent = LocalAgent(config, log)
    agent.start()
    print(f"local-agent 已启动：{config['agent_id']} -> {config['router']}", flush=True)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n正在停止 local-agent ...", flush=True)
        agent.stop()
    except Exception as exc:
        log.add("ERROR", f"运行时错误：{exc}")
        agent.stop()
        raise


if __name__ == "__main__":
    main()
