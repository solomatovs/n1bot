#!/bin/bash
# окружение разработчика: .venv, uv, репозитории nexus и BOBA_*
# запускать через: source dev.sh

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONF="$DIR/build/conf"

unset PYTHONHOME
unset PYTHONPATH

if [ -f "$CONF/pip.conf" ]; then
  export PIP_CONFIG_FILE="$CONF/pip.conf"
  echo "dev: pip.conf = $PIP_CONFIG_FILE"
fi

if [ -f "$CONF/uv.toml" ]; then
  export UV_CONFIG_FILE="$CONF/uv.toml"
  echo "dev: uv.toml = $UV_CONFIG_FILE"
fi

if [ -f "$CONF/ca-chain.crt" ]; then
  export REQUESTS_CA_BUNDLE="$CONF/ca-chain.crt"
  export CURL_CA_BUNDLE="$CONF/ca-chain.crt"
  export SSL_CERT_FILE="$CONF/ca-chain.crt"
  export PIP_CERT="$CONF/ca-chain.crt"
  export GIT_SSL_CAINFO="$CONF/ca-chain.crt"
  echo "dev: ca-chain = $CONF/ca-chain.crt"
fi

PACKAGES="make gcc python3-dev libkrb5-dev curl tar xz-utils gettext-base ca-certificates"

NEED_PACKAGES=""
for CMD in make gcc curl tar xz envsubst krb5-config; do
  if ! command -v "$CMD" > /dev/null; then
    NEED_PACKAGES="yes"
  fi
done

if [ -z "$NEED_PACKAGES" ]; then
  echo "dev: пакеты сборки уже стоят"
elif ! command -v apt-get > /dev/null; then
  echo "dev: apt-get нет, поставь сам: $PACKAGES" >&2
else
  echo "dev: ставлю пакеты сборки: $PACKAGES"
  apt-get update
  apt-get install -y $PACKAGES
fi

export PATH="$DIR/build/src/uv:$PATH"

if [ ! -d "$DIR/.venv" ]; then
  (cd "$DIR" && uv venv --python 3.11 --clear --no-managed-python)
fi

(cd "$DIR" && uv sync -v --system-certs)
OK=$?

if [ $OK -eq 0 ]; then
  source "$DIR/.venv/bin/activate"
  # activate перезаписывает PATH, поэтому uv возвращаем обратно
  export PATH="$DIR/build/src/uv:$PATH"
  echo "dev: окружение .venv активировано"
else
  echo "dev: ошибка подготовки окружения (.venv не активировано)" >&2
fi

# BOBA_* сюда не тянем: их подключает launch.json через envFile.
# в терминале при нужде: set -a; source .vscode/boba-debug.env; set +a
