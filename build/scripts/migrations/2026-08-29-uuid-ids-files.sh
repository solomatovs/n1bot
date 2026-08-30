#!/bin/sh
# Переименование файлов данных приложения по карте «старый int id|uuid» (вывод
# `select id, user_uuid from <schema>.users` до миграции схемы):
#   workspace/<id>.ext4(.lock) -> workspace/<uuid>.ext4(.lock), tool-logs/<id> -> tool-logs/<uuid>
# Вызов: 2026-08-29-uuid-ids-files.sh <карта> <каталог данных приложения>
set -eu

map=$1
data=$2

while IFS='|' read -r old new; do
    [ -n "$old" ] || continue
    for suffix in .ext4 .ext4.lock; do
        if [ -e "$data/workspace/$old$suffix" ]; then
            mv "$data/workspace/$old$suffix" "$data/workspace/$new$suffix"
            echo "workspace/$old$suffix -> $new$suffix"
        fi
    done
    if [ -d "$data/tool-logs/$old" ]; then
        mv "$data/tool-logs/$old" "$data/tool-logs/$new"
        echo "tool-logs/$old -> $new"
    fi
done < "$map"
