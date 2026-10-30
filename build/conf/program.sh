#!/bin/sh
# Программа boba на python: каталог программы — родитель bin/, в нём же интерпретатор
# (third/) и код (app/). Окружение python ставит этот скрипт, экземпляру оно не нужно:
# запуск — `<программа> serve --config <экземпляр>/conf/config.toml`
root=$(cd "$(dirname "$0")/.." && pwd -P)
export BOBA_RUNTIME="$root" PYTHONHOME="$root/third" PYTHONPATH="$root/app" \
    PYTHONNOUSERSITE=1 PYTHONUTF8=1 LD_LIBRARY_PATH="$root/third/lib"
exec "$root/third/bin/python3" -m @MODULE@ "$@"
