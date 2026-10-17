# boba на этой машине, пользователь ss

Памятка о том, что здесь устроено по-своему. Общее устройство — сборка,
конфигурация, запуск, песочница, отладка — описано в
[docs/admin-guide.md](../docs/admin-guide.md); здесь только местные значения и
отличия, со ссылками на его разделы.

## Две версии рядом

На машине живут две версии boba, и запускаются они по очереди.

Старая версия — ветка `dev`: чат chainlit, который сам исполняет инструменты, и
приложение studio. Её деревья — `compose/chainlit`, `compose/studio`,
`debug/chainlit` и `debug/studio`, базы PostgreSQL — `boba` у compose и
`boba_debug` у отладки.

Новая версия — ветка `refactoring/chainlit_agent` и её потомки: чат aid с
процессом-агентом и сервис инструментов boba-mcp; studio в неё не входит. Её
деревья — `compose/aid`, `compose/mcp`, `debug/aid` и `debug/mcp`, базы — `aid` у
compose и `aid_debug` у отладки. В VS Code новую версию запускают цели
`launch.json` «Aid: agent» (чат), «MCP: tools service» (boba-mcp), «Aid: aid +
mcp» (оба процесса сразу), «tool (aid)» и «pytest (aid): …».

Базу каждая версия выбирает сама. В site.toml есть оба ключа: `pg_database`
(`boba` или `boba_debug`) читает общий конфиг dev, `aid_database` (`aid` или
`aid_debug`) — общий конфиг новой версии. Поэтому один и тот же site.toml годится
чату обеих версий, и деревьям чата новой версии (`compose/aid`, `debug/aid`)
достаточно своего каталога `conf` с копией site.toml соответствующего дерева dev
(`compose/chainlit`, `debug/chainlit`). У `compose/mcp` и `debug/mcp` свой
site.toml. Перенос данных из базы dev в базу новой версии
описан в `build/scripts/migrations/2026-10-16-chat-agent-cutover/README.md`.

Контейнер чата новой версии называется `aid` (второй узел `aid2`): на это имя
рассчитаны карта доступа к PostgreSQL и прокси (раздел «Сеть»). Отладка чата
обеих версий слушает порт 8601, потому что порт задаёт тот же site.toml.

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
        chown -R 1000:0 /mnt/store/ss/docker/compose/boba/compose/mcp/data

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
| `aid` (чат новой версии) | 10.118.0.6 | 10.118.0.36 (`aid2`) |
| `boba-mcp` (только новая версия) | 10.118.0.30 | 10.118.0.31 (`boba-mcp2`) |
| `studio` (только dev) | 10.118.0.12 | — |

Адреса закреплены, потому что на них ссылается карта доступа между сетями
(`/mnt/store/ss/docker/compose/network-map.conf`, её применяют юниты
`docker-netmap-*`). Карта пускает к PostgreSQL только контейнеры с именами
`aid`, `studio` и `boba-mcp`: контейнер с другим именем или адресом до
базы не дойдёт и остановится с `PostgresError: ... couldn't get a connection`
(руководство, раздел 12). Вторые узлы обычно не запущены.

Снаружи приложения отдаёт контейнер `nginx`: `https://loshara.com/boba/` — чат
aid (контейнер `aid`), `/boba-mcp/health` — boba-mcp новой версии,
`/boba-studio/` — studio версии dev. Его конфигурация —
`/mnt/store/ss/docker/compose/nginx/conf/conf.d/locations/boba.conf`
(руководство, раздел 13).

Сетевые интерфейсы контейнерам выдаёт `lxc-user-nic`; число интерфейсов
пользователя ограничено в `/etc/lxc/lxc-usernet`. Отказ `Quota reached` при
старте контейнера означает, что лимит исчерпан: остановить ненужные контейнеры
либо поднять лимит.

Dev-сервер страницы workflow есть только у версии dev (`make -C build/studio
web-dev` в её дереве). По той же причине он подключается к сети `ss` под именем
`boba-web-dev`; это записано в `build/conf/local.mk`, а в site.toml отладки
studio стоит `workflow_page = "http://boba-web-dev:5173"`. Сборка новой версии
этих переменных не читает.

## Cgroup

Перемонтирование cgroup без `nsdelegate` (руководство, раздел 2) делает
`/usr/local/bin/boba-cgroup-setup.sh`, его запускает системный юнит
`boba-cgroup.service` при загрузке.

Юниты песочницы: `boba-sandbox@mcp` — для контейнера boba-mcp,
`boba-sandbox@debug` — для отладки; `boba-sandbox@studio` нужен только studio
версии dev. Перезапускать их под работающим приложением нельзя (руководство,
раздел 9).

В логе boba-mcp на каждом вызове есть строка `clone3 into cgroup refused ...
Function not implemented, falling back to fork`. Это не ошибка: seccomp-профиль
запрещает `clone3`, песочница переходит на запасной путь, вызов выполняется.

## Сторож

Таймер `docker-autoheal.timer` раз в минуту перезапускает контейнеры в
состоянии `unhealthy` (руководство, раздел 10). Контейнер, который намеренно
остановлен командой `docker compose stop`, он не трогает.

## Сборка

Базовые образы `dmp/python:3.11-dist`, `dmp/glibc:2.28`, `dmp/gcc:8.5.0` и
`dmp/nodejs:20` загружены в демон `ss`; `dmp/nodejs:22` там же нужен только
сборке studio версии dev. Машина общая: сборку запускают по одной, с `JOBS` не
больше 12.

Система машины — Arch Linux, в ней нет библиотек базового образа
(`libssl.so.1.1`, `libffi.so.6`), поэтому дерево релиза для запуска юнитами
собирают с `BUNDLE_OS_LIBS=1` (руководство, раздел 7).

## Отладка

Порты отладки: чат 8601 (у обеих версий: порт задаёт site.toml, одинаковый у
`debug/chainlit` и `debug/aid`), boba-mcp 8651, studio версии dev 8602. База отладки
новой версии — `aid_debug`, версии dev — `boba_debug`. Соседний пользователь
машины отлаживает свой чат на 8611. Keytab сервиса отладка читает из
`compose/<приложение>/conf/krb`: владелец файла — 625287, группа `ss`, режим
`0440`.

Каталоги `data/krb/cache` контейнеров закрыты от `ss` (режим `0700`); поиск
IDE по ним выдаёт «Permission denied» — это безвредно.
