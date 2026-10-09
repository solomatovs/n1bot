# boba на этой машине, пользователь ss

Памятка о том, что здесь устроено по-своему. Общее устройство — сборка,
конфигурация, запуск, песочница, отладка — описано в
[docs/admin-guide.md](../docs/admin-guide.md); здесь только местные значения и
отличия, со ссылками на его разделы.

## Пользователь и демон

Репозиторий — `/mnt/store/ss/docker/compose/boba`, владелец `ss` (uid 10003).
Контейнеры запускает rootless-демон Docker этого пользователя, юнит
`docker.service` в его пользовательском менеджере systemd; linger включён.
Диапазон subuid отображает пользователя 1000 контейнера в 625287 на машине:
этим номером владеют файлы в `compose/<приложение>/data`.

После `sudo su - ss` команды `docker` и `systemctl --user` работают сразу. В
неинтерактивном запуске (`sudo -u ss <команда>`) окружение задают явно:

    export XDG_RUNTIME_DIR=/run/user/10003
    export DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/10003/bus

Сменить владельца файла, созданного контейнером, без sudo можно из
пространства имён демона, где `ss` — это root:

    nsenter -U --preserve-credentials -t $(cat $XDG_RUNTIME_DIR/dockerd-rootless/child_pid) \
        chown -R 1000:0 /mnt/store/ss/docker/compose/boba/compose/studio/app_root

## Сеть

Демон работает без проброса портов (`DOCKERD_ROOTLESS_ROOTLESSKIT_PORT_DRIVER=none`):
строка `ports:` в compose-файле на этой машине ничего не даёт, и
`127.0.0.1:<порт>` контейнера с машины недоступен. Контейнеры доступны по
адресу в маршрутизируемой сети `ss` (10.118.0.0/16) и по имени — имена
контейнеров отдаёт DNS машины (юнит `docker-dns.service`).

Поэтому в `compose/<приложение>/.env` стоят `BOBA_NETWORK=ss`,
`BOBA_NETWORK_EXTERNAL=true` и закреплённые адреса:

| Контейнер | `BOBA_ADDRESS` | Второй узел, `BOBA_ADDRESS_2` |
|---|---|---|
| `chainlit` | 10.118.0.6 | 10.118.0.36 (`chainlit2`) |
| `boba-mcp` | 10.118.0.30 | 10.118.0.31 (`boba-mcp2`) |
| `studio` | 10.118.0.12 | — |

Адреса закреплены, потому что на них ссылается карта доступа между сетями
(`/mnt/store/ss/docker/compose/network-map.conf`, её применяют юниты
`docker-netmap-*`). Карта пускает к PostgreSQL только контейнеры с именами
`chainlit`, `studio` и `boba-mcp`: контейнер с другим именем или адресом до
базы не дойдёт и остановится с `PostgresError: ... couldn't get a connection`
(руководство, раздел 12). Вторые узлы обычно не запущены.

Снаружи приложения отдаёт контейнер `nginx`: `https://loshara.com/boba/`,
`/boba-mcp/health`, `/boba-studio/api/openapi.json`. Его конфигурация —
`/mnt/store/ss/docker/compose/nginx/conf/conf.d/locations/boba.conf`
(руководство, раздел 13).

Сетевые интерфейсы контейнерам выдаёт `lxc-user-nic`; число интерфейсов
пользователя ограничено в `/etc/lxc/lxc-usernet`. Отказ `Quota reached` при
старте контейнера означает, что лимит исчерпан: остановить ненужные контейнеры
либо поднять лимит.

Dev-сервер фронта (руководство, раздел 8, «Фронт») по той же причине
подключается к сети `ss` под именем `boba-web-dev`; это записано в
`build/conf/local.mk`, а в site.toml отладки studio стоит
`workflow_page = "http://boba-web-dev:5173"`.

## Cgroup

Перемонтирование cgroup без `nsdelegate` (руководство, раздел 2) делает
`/usr/local/bin/boba-cgroup-setup.sh`, его запускает системный юнит
`boba-cgroup.service` при загрузке.

Юниты песочницы: `boba-sandbox@mcp`, `boba-sandbox@studio` — для контейнеров,
`boba-sandbox@debug` — для отладки. Перезапускать их под работающим
приложением нельзя (руководство, раздел 9).

В логе boba-mcp на каждом вызове есть строка `clone3 into cgroup refused ...
Function not implemented, falling back to fork`. Это не ошибка: seccomp-профиль
запрещает `clone3`, песочница переходит на запасной путь, вызов выполняется.

## Сторож

Таймер `docker-autoheal.timer` раз в минуту перезапускает контейнеры в
состоянии `unhealthy` (руководство, раздел 10). Контейнер, который намеренно
остановлен командой `docker compose stop`, он не трогает.

## Сборка

Базовые образы `dmp/python:3.11-dist`, `dmp/glibc:2.28`, `dmp/gcc:8.5.0`,
`dmp/nodejs:20`, `dmp/nodejs:22` загружены в демон `ss`. Машина общая: сборку
запускают по одной, с `JOBS` не больше 12.

Система машины — Arch Linux, в ней нет библиотек базового образа
(`libssl.so.1.1`, `libffi.so.6`), поэтому дерево релиза для запуска юнитами
собирают с `BUNDLE_OS_LIBS=1` (руководство, раздел 7).

## Отладка

Порты отладки: чат 8601, studio 8602, boba-mcp 8651. Соседний пользователь
машины отлаживает свой чат на 8611. Keytab сервиса отладка читает из
`compose/<приложение>/conf/krb`: владелец файла — 625287, группа `ss`, режим
`0440`.

Каталоги `data/krb/cache` контейнеров закрыты от `ss` (режим `0700`); поиск
IDE по ним выдаёт «Permission denied» — это безвредно.
