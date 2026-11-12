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
export BOBA_DATA="$PROJECT_DIR/compose/data/debug"
export BOBA_MODELS="$PROJECT_DIR/build/out/models"
export BOBA_PORT="${BOBA_PORT:-8661}"
export BOBA_URL_PREFIX="${BOBA_URL_PREFIX:-/ss/boba}"
export BOBA_INSTANCE_ID=dev
export BOBA_MESSAGING=local
export BOBA_TOOL_LAUNCHER=process

mkdir -p "$BOBA_DATA/workspace"

exec "$PROJECT_DIR/.venv/bin/python" -m boba.chainlit.main \
  --config "$PROJECT_DIR/compose/conf/config.toml" "$@"
