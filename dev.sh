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

if [ -f "$CONF/npmrc" ]; then
  export NPM_CONFIG_FILE="$CONF/npmrc"
  echo "dev: npmrc = $NPM_CONFIG_FILE"
fi

if [ -f "$CONF/ca-chain.crt" ]; then
  export REQUESTS_CA_BUNDLE="$CONF/ca-chain.crt"
  export CURL_CA_BUNDLE="$CONF/ca-chain.crt"
  export SSL_CERT_FILE="$CONF/ca-chain.crt"
  export PIP_CERT="$CONF/ca-chain.crt"
  export GIT_SSL_CAINFO="$CONF/ca-chain.crt"
  echo "dev: ca-chain = $CONF/ca-chain.crt"
fi

PACKAGES="make gcc python3-dev libkrb5-dev curl tar xz-utils ca-certificates"

NEED_PACKAGES=""
for CMD in make gcc curl tar xz krb5-config; do
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

export PATH="$DIR/build/out/uv:$PATH"

if [ ! -d "$DIR/.venv" ]; then
  if ! (cd "$DIR" && uv venv --python 3.11 --clear --no-managed-python); then
    echo "dev: не удалось создать .venv на Python 3.11" >&2
    return 1 2> /dev/null || exit 1
  fi
fi

# python-oracledb в .venv идёт патченным колесом из build/out/oracledb
# (find-links в pyproject); без него uv sync не соберёт окружение
ORACLEDB_WHEELS="$DIR/build/out/oracledb"
ORACLEDB_PIN="$(sed -n 's/^[[:space:]]*"\(oracledb==[^"]*\)".*/\1/p' "$DIR/packages/infra/db/boba-db-oracle/pyproject.toml")"
ORACLEDB_CHECK=(uv pip install --python 3.11 --no-managed-python --dry-run
  --reinstall --no-deps --no-index --offline --find-links "$ORACLEDB_WHEELS" "$ORACLEDB_PIN")

if ! "${ORACLEDB_CHECK[@]}" > /dev/null 2>&1; then
  echo "dev: собираю колесо python-oracledb в $ORACLEDB_WHEELS"
  if ! make -C "$DIR/build" "$ORACLEDB_WHEELS"; then
    echo "dev: колесо python-oracledb не собралось, .venv не готовится" >&2
    return 1 2> /dev/null || exit 1
  fi

  if ! "${ORACLEDB_CHECK[@]}"; then
    echo "dev: в $ORACLEDB_WHEELS нет $ORACLEDB_PIN, совместимого с Python 3.11" >&2
    return 1 2> /dev/null || exit 1
  fi
fi

(cd "$DIR" && uv sync -v --python 3.11 --no-managed-python --system-certs --find-links build/out/oracledb)
OK=$?

if [ $OK -eq 0 ]; then
  source "$DIR/.venv/bin/activate"
  # activate перезаписывает PATH, поэтому uv возвращаем обратно
  export PATH="$DIR/build/out/uv:$PATH"
  echo "dev: окружение .venv активировано"
else
  echo "dev: ошибка подготовки окружения (.venv не активировано)" >&2
  return "$OK" 2> /dev/null || exit "$OK"
fi

# BOBA_* для отладки заданы в .vscode/launch.json.
