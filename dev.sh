#!/bin/bash

_dev_dir=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE:-$0}")" 2>/dev/null && pwd)

# если в PATH уже есть uv, то не добавляем его туда
case ":$PATH:" in
  *":$_dev_dir/build/artifacts/bin:"*) ;;
  *) PATH="$_dev_dir/build/artifacts/bin:$PATH"; export PATH ;;
esac

if ( set -eu; cd -- "$_dev_dir"; \
     [ -d .venv ] || uv venv --python 3.11 --clear --no-managed-python; uv sync -v --system-certs ); then
  . "$_dev_dir/.venv/bin/activate"
  # после source .venv/bin/activate сбрасывается переменная PATH, поэтому заново добавляю туда uv
  PATH="$_dev_dir/build/artifacts/bin:$PATH"
  echo "dev: окружение .venv активировано"
else
  echo "dev: ошибка подготовки окружения (.venv не активировано)" >&2
fi
