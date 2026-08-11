#!/bin/sh
# Делегирует boba.slice пользователю контейнера: в ней песочница создаёт
# per-run cgroup'ы с лимитами. Docker сам заводит slice от root, а лимиты
# пишет chainlit от своего uid — отсюда chown.
#
# Запускать от root один раз после загрузки хоста, до docker compose up:
#     sudo ./cgroup-init.sh
set -eu

SLICE=/sys/fs/cgroup/boba.slice
UID_GID="${BOBA_UID:-1000}:${BOBA_GID:-984}"

if [ "$(id -u)" -ne 0 ]; then
    echo "нужны права root: sudo $0" >&2
    exit 1
fi

if [ ! -d /sys/fs/cgroup/cgroup.controllers ] && [ ! -f /sys/fs/cgroup/cgroup.controllers ]; then
    echo "на хосте нет cgroup v2 (/sys/fs/cgroup/cgroup.controllers)" >&2
    exit 1
fi

mkdir -p "$SLICE" "$SLICE/boba-sandbox"

# контроллеры в subtree_control родителя: без них у leaf'ов не будет
# memory.max / cpu.max / pids.max
echo "+cpu +memory +pids" > "$SLICE/cgroup.subtree_control"

chown -R "$UID_GID" "$SLICE"

# правило общего предка cgroup v2: перенос процесса из session-*.scope в
# boba.slice требует прав на cgroup.procs их общего предка — корня
chown "$UID_GID" /sys/fs/cgroup/cgroup.procs

echo "$SLICE делегирован $UID_GID; контроллеры: $(cat "$SLICE/cgroup.controllers")"
