#!/bin/bash
set -eu

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR"

unset PYTHONHOME PYTHONPATH
export PYTHONUNBUFFERED=1
export PATH="$PROJECT_DIR/.venv/bin:$PATH"
export KRB5_CONFIG="$PROJECT_DIR/compose/conf/krb/krb5.conf"
export BOBA_BASE="$PROJECT_DIR/compose"
export BOBA_APP_ROOT="$PROJECT_DIR/packages/agents/boba-chainlit/assets"
export BOBA_DATA="$PROJECT_DIR/compose/data"
export BOBA_MODELS="$PROJECT_DIR/build/out/models"
export BOBA_PORT="${BOBA_PORT:-8601}"
export BOBA_URL_PREFIX="${BOBA_URL_PREFIX:-/boba}"
export BOBA_INSTANCE_ID=dev
export BOBA_MESSAGING=local
export BOBA_TOOL_LAUNCHER=process

exec "$PROJECT_DIR/.venv/bin/python" -m boba.chainlit.main \
  --config "$PROJECT_DIR/compose/conf/config.toml" "$@"
