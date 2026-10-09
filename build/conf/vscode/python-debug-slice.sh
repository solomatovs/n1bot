#!/bin/sh
set -eu
export XDG_RUNTIME_DIR="/run/user/$(id -u)"
export DBUS_SESSION_BUS_ADDRESS="unix:path=$XDG_RUNTIME_DIR/bus"
VENV_PYTHON="$(dirname "$(readlink -f "$0")")/../.venv/bin/python"
systemctl --user start boba-sandbox@debug.service
exec systemd-run --user --scope --quiet --collect --slice=boba-debug.slice -- "$VENV_PYTHON" "$@"
