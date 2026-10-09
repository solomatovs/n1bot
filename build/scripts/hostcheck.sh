#!/bin/sh
# Проверка машины перед раскладкой дерева приложения: чего не хватает пользователю
# и что именно попросить у root. Печатает все находки сразу; код 1 — есть то, без
# чего запуск невозможен.
#   hostcheck.sh <compose|debug|systemd> <sandbox|plain>
# compose — приложение в контейнере, debug и systemd — процесс на хосте;
# sandbox — приложение исполняет инструменты в песочнице, plain — нет.
set -u
where=$1
kind=$2
user=$(id -un)
uid=$(id -u)
failed=0

fail() {
    echo "  ✗ $1" >&2
    echo "      $2" >&2
    failed=1
}

ok() {
    echo "  ✓ $1"
}

if [ "$kind" = sandbox ]; then
    runtime=${XDG_RUNTIME_DIR:-/run/user/$uid}
    if [ -S "$runtime/bus" ] || [ -S "$runtime/systemd/private" ]; then
        ok "пользовательский systemd работает ($runtime)"
    else
        fail "пользовательский systemd недоступен: нет $runtime/bus" \
             "войти под $user сеансом с systemd (ssh, machinectl shell) либо попросить root: loginctl enable-linger $user"
    fi

    linger=$(loginctl show-user "$user" -p Linger --value 2>/dev/null)
    if [ "$linger" = yes ]; then
        ok "linger включён: юниты $user живут без входа в систему"
    else
        fail "linger выключен: юниты $user остановятся с последним выходом из системы" \
             "попросить root: loginctl enable-linger $user"
    fi

    controllers=/sys/fs/cgroup/user.slice/user-$uid.slice/user@$uid.service/cgroup.controllers
    missing=""
    for controller in cpu memory pids; do
        if ! grep -qw "$controller" "$controllers" 2>/dev/null; then
            missing="$missing $controller"
        fi
    done
    if [ -z "$missing" ]; then
        ok "контроллеры cgroup cpu, memory, pids делегированы user@$uid.service"
    else
        fail "user@$uid.service не получил контроллеры cgroup:$missing" \
             "попросить root: mkdir -p /etc/systemd/system/user@.service.d && printf '[Service]\\nDelegate=cpu cpuset io memory pids\\n' > /etc/systemd/system/user@.service.d/delegate.conf && systemctl daemon-reload, затем перезайти"
    fi

    if [ -r /dev/fuse ] && [ -w /dev/fuse ]; then
        ok "/dev/fuse доступен: образы workspace монтируются без root"
    else
        fail "/dev/fuse недоступен пользователю $user" \
             "попросить root: modprobe fuse; права устройства должны быть crw-rw-rw- (правило udev: KERNEL==\"fuse\", MODE=\"0666\")"
    fi

    userns=$(cat /proc/sys/kernel/unprivileged_userns_clone 2>/dev/null || echo 1)
    max_userns=$(cat /proc/sys/user/max_user_namespaces 2>/dev/null || echo 0)
    if [ "$userns" != 0 ] && [ "$max_userns" != 0 ]; then
        ok "непривилегированные user namespace разрешены: bwrap запускается без root"
    else
        fail "непривилегированные user namespace запрещены" \
             "попросить root: sysctl kernel.unprivileged_userns_clone=1 и user.max_user_namespaces больше нуля"
    fi
fi

if [ "$where" = compose ]; then
    if ! docker info > /dev/null 2>&1; then
        fail "docker недоступен пользователю $user" \
             "поставить rootless-демон: dockerd-rootless-setuptool.sh install (нужны записи $user в /etc/subuid и /etc/subgid — их добавляет root)"
    elif docker info --format '{{.SecurityOptions}}' 2>/dev/null | grep -q name=rootless; then
        ok "docker работает в режиме rootless"
        if grep -q "^$user:" /etc/subuid 2>/dev/null && grep -q "^$user:" /etc/subgid 2>/dev/null; then
            ok "у $user есть диапазоны subuid и subgid"
        else
            fail "у $user нет диапазона в /etc/subuid или /etc/subgid" \
                 "попросить root: usermod --add-subuids 100000-165535 --add-subgids 100000-165535 $user"
        fi
    else
        ok "docker работает обычным (не rootless) демоном"
    fi

    if [ "$kind" = sandbox ]; then
        if grep -E '^cgroup2 /sys/fs/cgroup ' /proc/mounts | grep -qw nsdelegate; then
            fail "cgroup2 смонтирована с nsdelegate: контейнер не сможет переносить процессы в поддерево песочницы" \
                 "попросить root: mount -t cgroup2 -o remount,rw,nosuid,nodev,noexec,relatime cgroup2 /sys/fs/cgroup — и повторять это при загрузке"
        else
            ok "cgroup2 смонтирована без nsdelegate"
        fi
    fi
fi

exit $failed
