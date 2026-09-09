#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
: "${BIGBASE_DEPLOYMENT_FILE:?Private deployment configuration required}"
case "${BIGBASE_ENV:-}" in staging|production) ;; *) exit 64 ;; esac
if [[ "${BIGBASE_LOCAL_HTTP:-}" == 1 ]]; then exit 64; fi
export PYTHONPATH="$PWD/backend"
export PYTHONDONTWRITEBYTECODE=1
exec .venv/bin/uvicorn bigbase.production:create_app --factory \
  --host 127.0.0.1 --port "${BIGBASE_HTTP_PORT:-18770}" --workers 1 \
  --proxy-headers --forwarded-allow-ips=127.0.0.1
