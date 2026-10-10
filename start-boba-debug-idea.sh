(
  set -eu

  cd /mnt/store/alexeyk500/docker/compose/boba
  PROJECT_DIR="$PWD"

  make -C "$PROJECT_DIR/build/chainlit" \
    debug \
    DEBUG_PORT=8611 \
    DEBUG_PREFIX=/alexeyk500/boba-debug

  export PATH="$PROJECT_DIR/.venv/bin:$PROJECT_DIR/runtime/third/bin:$PATH"
  export LD_LIBRARY_PATH="$PROJECT_DIR/runtime/third/lib"

  cd "$PROJECT_DIR/debug/chainlit"

  "$PROJECT_DIR/.vscode/python-debug-slice.sh" \
    "$PROJECT_DIR/packages/agents/boba-chainlit/src/boba/chainlit/main.py" \
    --config "$PROJECT_DIR/debug/chainlit/conf/config.toml"
)