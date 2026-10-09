#!/bin/bash
# Владельцы дерева compose: конфигурацию контейнер читает через группу, в данные пишет
# как владелец. Пользователь контейнера на машине — номер из subuid, поэтому смена
# владельца идёт в user namespace с тем же отображением, что у rootless-демона: там
# текущий пользователь — 0, а пользователь контейнера — его собственный номер.
# Трогаются только файлы текущего пользователя: расставленное раньше не меняется.
# Аргументы: каталог дерева, uid:gid процесса в контейнере.
set -euo pipefail

tree="$1"
owner="$2"
uid="${owner%%:*}"
gid="${owner##*:}"

if ! docker info --format '{{.SecurityOptions}}' 2>/dev/null | grep -q name=rootless; then
    echo "    владельцы: демон docker не rootless — пользователь $owner контейнера есть тот же номер на машине;"
    echo "    попросить root: chgrp -R $gid $tree/conf && chmod -R g+rX,o-rwx $tree/conf && chown $owner $tree/data $tree/data/*"
    exit 0
fi

unshare --user --map-root-user --map-auto /bin/bash -euc '
    tree="$1"; uid="$2"; gid="$3"
    find "$tree/conf" -uid 0 -exec chgrp "$gid" {} +
    find "$tree/conf" -uid 0 -type d -exec chmod g+rxs,o-rwx {} +
    find "$tree/conf" -uid 0 -type f -exec chmod g+r,o-rwx {} +
    for dir in "$tree/data" "$tree/app_root"; do
        if [ -d "$dir" ]; then
            find "$dir" -maxdepth 1 -uid 0 -type d -exec chown "$uid:0" {} + -exec chmod g+rwxs {} +
        fi
    done
' composeown "$tree" "$uid" "$gid"
echo "    владельцы: $tree/conf читает группа $gid контейнера, в $tree/data пишет его пользователь $uid"
