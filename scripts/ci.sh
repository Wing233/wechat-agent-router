#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

python -m py_compile \
  app.py \
  agent_broker.py \
  executor_client.py \
  executor/server.py \
  governance.py \
  registry.py \
  store.py

python -m pytest -q tests
