#!/bin/sh
# Делегированный cgroup v2 для песочницы boba: одно поддерево на все запуски.
#
# Приложение переносит каждый запуск инструмента в собственный leaf, поэтому
# ему нужна запись в самом поддереве и в cgroup.procs общего предка — по
# правилу общего предка cgroup v2. Поддерево одно и то же для запуска из IDE
# (BOBA_CGROUP_BASE=/sys/fs/cgroup/boba.slice/boba-sandbox) и для контейнера
# (том /sys/fs/cgroup/boba.slice:/cgroup, BOBA_CGROUP_BASE=/cgroup/boba-sandbox).
#
# Имя boba.slice выбрано под systemd-драйвер docker: cgroup_parent принимает
# только *.slice, поэтому контейнер ложится рядом с поддеревом песочницы.
set -eu

USER_NAME="${BOBA_USER:-boba}"
SLICE=/sys/fs/cgroup/boba.slice
BASE="$SLICE/boba-sandbox-studio"
CONTROLLERS="cpu memory pids"

if [ ! -d /sys/fs/cgroup/cgroup.controllers ] && [ ! -f /sys/fs/cgroup/cgroup.controllers ]; then
    echo "нет cgroup v2 в /sys/fs/cgroup" >&2
    exit 1
fi

id "$USER_NAME" > /dev/null 2>&1 || {
    echo "нет пользователя $USER_NAME (задай BOBA_USER)" >&2
    exit 1
}

mkdir -p "$SLICE" "$BASE"

# контроллеры нужны и в срезе, и в поддереве: лимиты ставятся на leaf'ах
for target in /sys/fs/cgroup "$SLICE"; do
    for controller in $CONTROLLERS; do
        if ! grep -qw "$controller" "$target/cgroup.subtree_control" 2>/dev/null; then
            echo "+$controller" > "$target/cgroup.subtree_control" || true
        fi
    done
done

# запись в общего предка: без неё перенос процесса в leaf даёт EACCES
chown "$USER_NAME" "$SLICE" "$SLICE/cgroup.procs" "$SLICE/cgroup.subtree_control"
chown -R "$USER_NAME" "$BASE"

echo "cgroup готов: $BASE (пользователь $USER_NAME)"
echo "  IDE:     BOBA_CGROUP_BASE=$BASE"
echo "  compose: том $SLICE:/cgroup, BOBA_CGROUP_BASE=/cgroup/boba-sandbox-studio"
