#!/bin/sh

_dev_dir=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE:-$0}")" 2>/dev/null && pwd)

case ":$PATH:" in
  *":$_dev_dir/build/artifacts/bin:"*) ;;
  *) PATH="$_dev_dir/build/artifacts/bin:$PATH"; export PATH ;;
esac

if ( set -eu; cd -- "$_dev_dir"; \
     [ -d .venv ] || uv venv --python 3.11 --no-managed-python; \
     uv sync --offline ); then
  . "$_dev_dir/.venv/bin/activate"
  echo "dev: окружение .venv активировано"
else
  echo "dev: ошибка подготовки окружения (.venv не активировано)" >&2
fi
