#!/bin/sh
# Делегированные cgroup v2 для песочницы boba: по поддереву на каждого хозяина.
#
# Приложение переносит каждый запуск инструмента в собственный leaf, поэтому
# ему нужна запись в самом поддереве и в cgroup.procs общего предка — по
# правилу общего предка cgroup v2. Хозяева разные, поэтому поддерева два:
#
#   контейнер (compose, root):  /sys/fs/cgroup/boba.slice/boba-sandbox
#                               том /sys/fs/cgroup/boba.slice:/cgroup,
#                               BOBA_CGROUP_BASE=/cgroup/boba-sandbox
#   IDE (launch.json, dev):     /sys/fs/cgroup/boba
#                               BOBA_CGROUP_BASE=/sys/fs/cgroup/boba
#
# Имя boba.slice выбрано под systemd-драйвер docker: cgroup_parent принимает
# только *.slice, поэтому контейнер ложится рядом с поддеревом песочницы.
# Поддерево IDE лежит в корне: процесс из user.slice переносит запуск через
# общего предка — корень, и записи в его cgroup.procs хватает.
set -eu

ROOT=/sys/fs/cgroup
SLICE="$ROOT/boba.slice"
CONTAINER_USER="${BOBA_USER:-root}"
CONTAINER_BASE="$SLICE/boba-sandbox"
IDE_USER="${BOBA_IDE_USER:-${SUDO_USER:-$(id -un)}}"
IDE_BASE="$ROOT/boba"
CONTROLLERS="cpu memory pids"

if [ ! -e "$ROOT/cgroup.controllers" ]; then
    echo "нет cgroup v2 в $ROOT" >&2
    exit 1
fi

for account in "$CONTAINER_USER" "$IDE_USER"; do
    id "$account" > /dev/null 2>&1 || {
        echo "нет пользователя $account (задай BOBA_USER / BOBA_IDE_USER)" >&2
        exit 1
    }
done

enable_controllers() {
    target="$1"

    for controller in $CONTROLLERS; do
        if grep -qw "$controller" "$target/cgroup.subtree_control" 2>/dev/null; then
            continue
        fi

        echo "+$controller" > "$target/cgroup.subtree_control" || true
    done
}

mkdir -p "$SLICE" "$CONTAINER_BASE" "$IDE_BASE"

# контроллеры нужны на каждом уровне до leaf'а: лимиты ставятся на leaf'ах
enable_controllers "$ROOT"
enable_controllers "$SLICE"
enable_controllers "$IDE_BASE"

# общий предок контейнерного поддерева — сам срез: контейнер лежит в нём
chown "$CONTAINER_USER" "$SLICE" "$SLICE/cgroup.procs" "$SLICE/cgroup.subtree_control"
chown -R "$CONTAINER_USER" "$CONTAINER_BASE"

# общий предок IDE-поддерева — корень: процесс запускается из user.slice
chown "$IDE_USER" "$ROOT/cgroup.procs"
chown -R "$IDE_USER" "$IDE_BASE"

echo "cgroup готов"
echo "  compose ($CONTAINER_USER): том $SLICE:/cgroup, BOBA_CGROUP_BASE=/cgroup/boba-sandbox"
echo "  IDE     ($IDE_USER): BOBA_CGROUP_BASE=$IDE_BASE"
