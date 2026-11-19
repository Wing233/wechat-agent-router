#!/usr/bin/env python3
"""Token-protected local bridge for the read-only ncm-cli music plugin.

Run this on the machine where ncm-cli is installed and logged in (usually the
Windows desktop). The Router container calls POST /run with an argv allowlist;
the bridge never accepts write/playback commands and never uses a shell for
untrusted text.

    set NCM_BRIDGE_TOKEN=change-me
    python scripts/ncm_bridge.py --host 0.0.0.0 --port 18888

For Docker on the same Windows host, configure the plugin with
``bridge_url=http://host.docker.internal:18888``. Bind 0.0.0.0 only on a
trusted network/firewall; the token is the only application-level gate.
"""
from __future__ import annotations

import argparse
import hmac
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from plugins.music.client import MusicError, run_ncm_cli  # noqa: E402
from plugins.music.readonly import ReadonlyError, validate_args  # noqa: E402

MAX_BODY = 64 * 1024


class BridgeHandler(BaseHTTPRequestHandler):
    server_version = "ncm-bridge/1.0"

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/health":
            self._json(200, {
                "ok": True,
                "readonly": True,
                "service": "ncm-cli-bridge",
                "commands": len(self.server.readonly_commands),  # type: ignore[attr-defined]
            })
            return
        self._json(404, {"ok": False, "error": {"code": "not_found", "message": "not found"}})

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/run":
            self._json(404, {"ok": False, "error": {"code": "not_found", "message": "not found"}})
            return
        if not self._authorized():
            self._json(401, {"ok": False, "error": {"code": "unauthorized", "message": "invalid bridge token"}})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_BODY:
            self._json(413, {"ok": False, "error": {"code": "body_too_large", "message": "request body is empty or too large"}})
            return
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._json(400, {"ok": False, "error": {"code": "invalid_json", "message": "request body must be JSON"}})
            return
        if not isinstance(body, dict):
            self._json(400, {"ok": False, "error": {"code": "invalid_json", "message": "request body must be an object"}})
            return
        try:
            args = validate_args(body.get("args"))
            payload = run_ncm_cli(self.server.binary, args, self.server.timeout)  # type: ignore[attr-defined]
        except ReadonlyError as exc:
            self._json(403, {"ok": False, "error": {"code": "forbidden_command", "message": str(exc)}})
            return
        except MusicError as exc:
            self._json(502, {"ok": False, "error": {"code": exc.code, "message": str(exc)}})
            return
        except Exception as exc:  # pragma: no cover - defensive boundary
            self._json(500, {"ok": False, "error": {"code": "bridge_error", "message": str(exc)[:300]}})
            return
        self._json(200, {"ok": True, "data": payload})

    def _authorized(self) -> bool:
        expected = self.server.token  # type: ignore[attr-defined]
        if not expected:
            return False
        header = self.headers.get("Authorization") or ""
        prefix = "Bearer "
        if not header.startswith(prefix):
            return False
        return hmac.compare_digest(header[len(prefix):], expected)

    def _json(self, status: int, payload: dict) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write("[ncm-bridge] " + (fmt % args) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only ncm-cli HTTP bridge")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18888)
    parser.add_argument("--token", default=os.environ.get("NCM_BRIDGE_TOKEN", ""))
    parser.add_argument("--bin", default=os.environ.get("NCM_CLI_BIN", "ncm-cli"))
    parser.add_argument("--timeout", type=int, default=30)
    args = parser.parse_args()
    if not args.token:
        print("NCM_BRIDGE_TOKEN (or --token) is required; refusing to expose an unauthenticated bridge.", file=sys.stderr)
        return 2
    if args.host not in ("127.0.0.1", "::1", "localhost"):
        print(f"[ncm-bridge] listening on {args.host}; keep the port behind a firewall.", file=sys.stderr)
    server = ThreadingHTTPServer((args.host, args.port), BridgeHandler)
    server.token = args.token
    server.binary = args.bin
    server.timeout = max(1, args.timeout)
    from plugins.music.readonly import READ_ONLY_COMMANDS
    server.readonly_commands = READ_ONLY_COMMANDS
    print(f"[ncm-bridge] read-only bridge on http://{args.host}:{args.port}", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
