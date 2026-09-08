#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
export PYTHONPATH="$PWD/backend"
export BIGBASE_LOCAL_HTTP=1
exec .venv/bin/uvicorn bigbase.api:create_app --factory --host 127.0.0.1 --port 18765 --workers 1
