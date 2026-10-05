#!/bin/sh
# Перенос образов workspace пользователей из чата в сервис boba-mcp по карте
# «id пользователя чата|логин» (вывод `select id, identifier from <schema>.users`):
#   <данные чата>/workspace/<id>.ext4 -> <данные сервиса>/workspace/<id сервиса>.ext4
# Сервис держит workspace по id, выведенному из логина: uuid5(NAMESPACE_URL,
# "boba-mcp:<логин>"). Ключи вложений в таблицах чата не меняются: владельца
# файла сервис берёт из токена входа, из ключа уходят только тред, каталог и имя.
# Образ, который у сервиса для пользователя уже есть, не перезаписывается.
# Вызов: 2026-10-05-workspace-to-mcp.sh <карта> <данные чата> <данные сервиса>
set -eu

map=$1
chat=$2
service=$3

mkdir -p "$service/workspace"

while IFS='|' read -r old login; do
    [ -n "$old" ] || continue
    source="$chat/workspace/$old.ext4"
    if [ ! -e "$source" ]; then
        continue
    fi

    new=$(python3 -c 'import sys, uuid; print(uuid.uuid5(uuid.NAMESPACE_URL, "boba-mcp:" + sys.argv[1]))' "$login")
    target="$service/workspace/$new.ext4"
    if [ -e "$target" ]; then
        echo "skipped $login: $target already exists, $source is left in place" >&2
        continue
    fi

    mv "$source" "$target"
    rm -f "$source.lock"
    echo "workspace of $login: $old.ext4 -> $new.ext4"
done < "$map"
