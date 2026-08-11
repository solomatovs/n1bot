#!/bin/bash

_dev_dir=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE:-$0}")" 2>/dev/null && pwd)

# чужой PYTHONHOME (например от airflow) уводит stdlib любого запускаемого
# python в чужой префикс: сборочный интерпретатор uv падает на encodings
if [ -n "${PYTHONHOME:-}" ] || [ -n "${PYTHONPATH:-}" ]; then
  echo "dev: снимаю PYTHONHOME=${PYTHONHOME:-} PYTHONPATH=${PYTHONPATH:-}" >&2
  unset PYTHONHOME PYTHONPATH
fi

# если в PATH уже есть uv, то не добавляем его туда
case ":$PATH:" in
  *":$_dev_dir/build/src/uv:"*) ;;
  *) PATH="$_dev_dir/build/src/uv:$PATH"; export PATH ;;
esac

if ( set -eu; cd -- "$_dev_dir"; \
     [ -d .venv ] || uv venv --python 3.11 --clear --no-managed-python; uv sync -v --system-certs ); then
  . "$_dev_dir/.venv/bin/activate"
  # после source .venv/bin/activate сбрасывается переменная PATH, поэтому заново добавляю туда uv
  # third/bin строго после .venv/bin: там лежит свой python3, который без PYTHONHOME не стартует
  PATH="$_dev_dir/build/src/uv:$_dev_dir/.venv/bin:$_dev_dir/release/current/third/bin:$PATH"
  export PATH
  echo "dev: окружение .venv активировано"
else
  echo "dev: ошибка подготовки окружения (.venv не активировано)" >&2
fi

if [ ! -d "$_dev_dir/release/current/third/bin" ]; then
  echo "dev: нет release/current/third/bin — bwrap и fuse2fs возьмутся системные (make -C build extract)" >&2
fi

# тот же env, что launch.json отдаёт отладчику: конфиг один, и терминальный
# pytest должен видеть его так же, как IDE
_dev_env="$_dev_dir/.vscode/boba-debug.env"
if [ -f "$_dev_env" ]; then
  set -a
  . "$_dev_env"
  set +a
  echo "dev: BOBA_* из $(basename -- "$_dev_env"); конфиг $BOBA_CONFIG_PATH"
else
  echo "dev: нет $_dev_env — BOBA_* не выставлены" >&2
fi
