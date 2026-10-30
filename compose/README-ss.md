# boba на этой машине, пользователь ss

Памятка о том, что здесь устроено по-своему. Общее устройство — сборка,
конфигурация, запуск, песочница, отладка — описано в
[docs/admin-guide.md](../docs/admin-guide.md); здесь только местные значения и
отличия, со ссылками на его разделы.

## Версия и базы

На машине живёт одна версия boba — ветка `refactoring/chainlit_agent` и её
потомки: чат aid, служба агента boba и сервис инструментов boba-mcp. Её
деревья — `compose/aid`, `compose/mcp`, `compose/boba`, `debug/aid`,
`debug/mcp` и `debug/boba`, базы PostgreSQL — `aid` у compose и
`aid_<разработчик>` у отладки (`aid_ss` у `ss`); базу выбирает ключ `dbname`
секций `[postgres]` и `[postgres_history]` в `config.toml` каждого дерева
(руководство, раздел 8, «Среда разработчика»). На стенде каждое приложение идёт
одним контейнером: `aid`, `boba-mcp`, `boba`; в `compose/<приложение>/conf`
лежат `config.toml`, `krb/` и у boba-mcp `prompts/`. Вход во все
внешние системы стенда описан таблицами `auth` в конфигах: в PostgreSQL —
kerberos по keytab (`[postgres.auth]` и `[postgres_history.auth]`, `method =
"kerberos_keytab"`), к модели и в Confluence — токен (`method = "bearer"`), каталог LDAP — служебный simple bind (`[ldap.auth]`,
`method = "simple"`), почта — логин SMTP (`[mail.auth]`, `method =
"password"`). В VS Code её запускают цели `launch.json` «Aid: chat» (чат, агент дочерним
процессом или службой), «MCP: tools service» (boba-mcp, тела процессом или в
песочнице), «Boba: agent service» (`boba serve`), «Aid: aid + mcp + boba» (все
три сразу), «tool» (одно тело через `boba.runtime.toolcli`) и «pytest: …».

Прежняя версия (ветка `dev`, чат chainlit и studio, базы `boba` и `boba_debug`)
выведена 2026-10-24: её треды, история, соединения, база знаний и индекс
перенесены в `aid` и `aid_debug` (теперь `aid_ss`) скриптами
`build/scripts/migrations/2026-10-24-history-merge`, базы `boba`, `boba_debug` и
`solomatovs` удалены, их дампы лежат в `/mnt/nas/ss/pg-backup-2026-10-24`.

Базы `aid` и `aid_<разработчик>` — системные. Рабочая база чат-бота, куда
пользователи ходят своим логином через соединение `pg-krb-delegate`
(Kerberos-делегирование), — `playground` у стенда и `playground_<разработчик>` у
отладки: владелец `boba-svc`, группа `adusers` может создавать в ней схемы и
объекты в `public`. Соединения ClickHouse у стенда смотрят в `default`, у отладки
— в `playground_<разработчик>` кластера `gpn`. Базы разработчика заводит
`make -C build dev-db` (руководство, раздел 8).

Индексаторы и скраперы ix (`compose/ix-*`, `compose/cfl-indexer`,
`compose/*-meta-scraper`) пишут в схему `ix` базы `aid`; схему они создают сами
командой `upgrade`, ядро `ix-core` — первым, `cfl-indexer` — после `ix-fts`.
Модель эмбеддингов `ix-vector` и `ix-search-lab` берут из `runtime/models/fastembed`.
Отладочные индексаторы (`debug/ix-*`, `debug/cfl-indexer`, `debug/*-meta-scraper`,
цели `launch.json`) пишут в базу разработчика.

Контейнер чата называется `aid`: на это имя рассчитаны карта доступа к
PostgreSQL и прокси (раздел «Сеть»). Отладка чата
обеих версий слушает порт 8601, потому что порт задаёт ключ `[env] port`
конфига `debug/aid/conf/config.toml`.

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

| Контейнер | `BOBA_ADDRESS` |
|---|---|
| `aid` (чат) | 10.118.0.6 |
| `boba-mcp` (сервис инструментов) | 10.118.0.30 |
| `boba` (служба агента) | 10.118.0.32 |

Адреса закреплены, потому что на них ссылается карта доступа между сетями
(`/mnt/store/ss/docker/compose/network-map.conf`, её применяют юниты
`docker-netmap-*`). Карта пускает к PostgreSQL только контейнеры с именами
`aid`, `boba-mcp` и `boba`: контейнер с другим именем или адресом до
базы не дойдёт и остановится с `PostgresError: ... couldn't get a connection`
(руководство, раздел 12).

Снаружи приложения отдаёт контейнер `nginx`: `https://loshara.com/aid/` — чат
(контейнер `aid`), `/mcp/` — boba-mcp (`/mcp/health`, серверы `/mcp/chat`,
`/mcp/all`, `/mcp/search`), `/boba/` — служба агента (`/boba/health`,
ACP на `/boba/acp`). Каждое приложение само отвечает под своим префиксом, nginx
передаёт путь как есть, без `rewrite`; метаданные OAuth
`/.well-known/…/mcp` и `/.well-known/…/boba` идут в свои сервисы. Чат ходит в
boba-mcp и службу агента тоже через nginx, по https. Браузер со старой ссылкой
чата `/boba/…` (заголовок `Sec-Fetch-Dest: document`) nginx уводит ответом 302 на
`/aid/…` с тем же остатком пути; клиенты службы агента этот заголовок не шлют.
Конфигурация nginx —
`/mnt/store/ss/docker/compose/nginx/conf/conf.d/locations/boba.conf`
(руководство, раздел 14).

Сетевые интерфейсы контейнерам выдаёт `lxc-user-nic`; число интерфейсов
пользователя ограничено в `/etc/lxc/lxc-usernet`. Отказ `Quota reached` при
старте контейнера означает, что лимит исчерпан: остановить ненужные контейнеры
либо поднять лимит.

Dev-сервер страницы workflow есть только у версии dev (`make -C build/studio
web-dev` в её дереве). По той же причине он подключается к сети `ss` под именем
`boba-web-dev`; это записано в `build/conf/local.mk`, а в конфиге отладки
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

Порты отладки: чат 8601, служба агента 8661, boba-mcp 8651. Снаружи отладка
`ss` отвечает под префиксом разработчика: `https://loshara.com/ss/aid/`,
`/ss/boba/`, `/ss/mcp/` (ключ `[env] url_prefix` конфига каждого дерева
`debug/<приложение>/conf/config.toml`); чат отладки ходит в сервис и службу агента через nginx,
как на стенде. Блоки nginx отладки — файл
`/mnt/store/ss/docker/compose/nginx/conf/conf.d/locations/dev-ss.conf`;
разработчика добавляют своим файлом `dev-<разработчик>.conf` (руководство,
раздел 14, «Отладка разработчиков»). К процессам на хосте nginx ходит адресом
`host.docker.internal` (10.0.3.1), и пускает его туда только правило
`ip saddr 10.118.0.2 tcp dport { … } accept` в
`/usr/local/sbin/rootless-docker-host-net`: порты отладки разработчика
(у `ss` — 8601, 8651, 8661) вписывают туда от root и перезапускают
`rootless-docker-host-net.service`. База отладки — `aid_ss`, рабочие базы
соединений — `playground_ss` в postgres и ClickHouse. Соседний пользователь машины
`alexeyk500` отлаживает свой чат на 8611; его база новой версии — `aid_alexeyk500`.
Тестовые базы называются `aid_ss_<тест>_…`, объекты ClickHouse и Oracle тестов —
`ss_<имя>_…`. Keytab сервиса отладка читает из
`compose/<приложение>/conf/krb`: владелец файла — 625287, группа `ss`, режим
`0440`.

Каталоги `data/krb/cache` контейнеров закрыты от `ss` (режим `0700`); поиск
IDE по ним выдаёт «Permission denied» — это безвредно.
