# boba под пользователем ss: эксплуатация и отладка

Заметка для владельца хоста. Здесь описано, как устроен запуск трёх сервисов
boba (chainlit, boba-mcp, studio) под пользователем `ss`, что с ними можно и
нельзя делать и как отлаживать код из IDE. Root и sudo пользователю `ss` для
всего перечисленного не нужны.

## Что где работает

Контейнеры запускает rootless-демон docker пользователя `ss` (uid 10003).
Демон — это user-юнит `docker.service`; у `ss` включён linger, поэтому демон и
контейнеры поднимаются при загрузке хоста без входа в систему.

Внутри контейнеров процессы идут от `1000:1000` (`BOBA_UID`, `BOBA_GID` в
`compose/<приложение>/.env`). Rootless-демон отображает идентификаторы
контейнера на subuid пользователя `ss`: uid и gid 1000 контейнера — это 625287
на хосте, а uid 0 контейнера — сам `ss`. Поэтому файлы, которые контейнер
создаёт в `data/`, на хосте принадлежат 625287.

Команды `docker`, `docker compose` и `systemctl --user` работают сразу после
`sudo su - ss`: docker ходит в демон через контекст `rootless`, а окружение
user-сессии (`XDG_RUNTIME_DIR`, `DBUS_SESSION_BUS_ADDRESS`) интерактивному шеллу
выставляет `~/.bashrc` пользователя `ss`. В неинтерактивном запуске
(`sudo -u ss <команда>`, cron) этих переменных нет, и их задают явно:

    export XDG_RUNTIME_DIR=/run/user/$(id -u)
    export DBUS_SESSION_BUS_ADDRESS=unix:path=$XDG_RUNTIME_DIR/bus

## Запуск и остановка

Каждое приложение — отдельный compose-проект в своём каталоге:

    cd /mnt/store/ss/docker/compose/boba/compose/mcp
    docker compose up -d mcp        # в chainlit — сервис chainlit, в studio — studio
    docker compose stop mcp
    docker compose up -d --force-recreate mcp   # после правки .env или docker-compose.yml
    docker logs -f boba-mcp

Сервисы `mcp2` и `chainlit2` в тех же файлах — вторые узлы, обычно не запущены.
Проверка снаружи: `https://loshara.com/boba/`, `/boba-mcp/health`,
`/boba-studio/api/openapi.json` отвечают 200.

## Cgroup песочницы и user-юниты boba-sandbox@

boba-mcp и studio исполняют инструменты в песочнице и на каждый вызов создают
отдельный cgroup с лимитами памяти, cpu и числа процессов. Контейнеру без
привилегий для этого нужно поддерево cgroup, в которое он вправе писать. Его
даёт user-юнит `boba-sandbox@<приложение>.service` (шаблон
`~/.config/systemd/user/boba-sandbox@.service`): systemd делегирует юниту
поддерево, юнит создаёт в нём каталог `sandbox` и отдаёт его группе 625287
(gid 1000 контейнера). Этот каталог compose монтирует в контейнер как
`/cgroup/boba-sandbox-<приложение>`; путь на хосте записан в
`BOBA_SANDBOX_CGROUP` файла `.env`. Сам контейнер помещён в соседний cgroup того
же слайса (`cgroup_parent: boba-<приложение>.slice`) — ядро разрешает перенос
процесса только под общим предком, доступным на запись.

Юниты `boba-sandbox@mcp` и `boba-sandbox@studio` включены и стартуют раньше
демона docker. Порядок после сбоя: сначала юнит, затем контейнер.

    systemctl --user status 'boba-sandbox@*'
    systemctl --user start boba-sandbox@mcp.service

Чего делать нельзя: перезапускать или останавливать `boba-sandbox@mcp` или
`boba-sandbox@studio`, пока работает соответствующий контейнер. Перезапуск
удаляет и создаёт каталог `sandbox` заново, а в контейнере остаётся
смонтированным старый, уже удалённый: каждый вызов инструмента начнёт падать с
`CgroupError`. Если юнит всё же перезапущен, пересоздайте контейнер
(`docker compose up -d --force-recreate <сервис>`). По той же причине после
`systemctl --user daemon-reload` юниты трогать не нужно: правка шаблона
применится при следующем старте.

Скрипт `/usr/local/bin/boba-cgroup-setup.sh` (root, при загрузке) делает одно:
перемонтирует cgroup2 без `nsdelegate`. Root-скрипта подготовки cgroup в
репозитории больше нет; шаблон юнита лежит и в репозитории —
`build/conf/boba-sandbox@.service`, схема целиком описана в
`docs/admin-guide.md`, раздел 7.

В логе boba-mcp на каждом вызове есть строка `clone3 into cgroup refused ...
Function not implemented, falling back to fork`. Это не ошибка: seccomp-профиль
контейнера запрещает `clone3`, и песочница переходит на запасной путь (fork и
запись в `cgroup.procs`), вызов выполняется штатно.

## Данные, конфиги и их владельцы

Всё лежит в `compose/<приложение>/`:

- `conf/` — конфиги, владелец `ss`, группа 625287, каталоги `2750`, файлы
  `0640`. `ss` правит файлы, контейнер читает их через группу, остальным
  доступа нет. Новые файлы наследуют группу от каталога.
- `conf/krb/*.keytab` — владелец 625287, группа `ss`, режим `0440`. Читают
  процесс контейнера (как владелец) и `ss` (через группу — это нужно отладке
  на хосте); остальным доступа нет.
- `data/` и `studio/app_root/` — владелец 625287, группа `ss`. Сюда контейнер
  пишет: рабочие образы пользователей `data/workspace/*.ext4`, журналы
  инструментов `data/tool-logs/`, кэши билетов `data/krb/`.
- `models/`, `third/`, `app_root/` — владелец `ss`, только чтение для
  контейнера; их перекладывает `make -C build dev APP=<приложение>`.

Файлы, созданные контейнером, `ss` читает, но изменить или удалить напрямую
может не все (у новых файлов нет записи для группы). Сменить владельца или
права без sudo можно из user namespace демона, где `ss` — это root, а
контейнерный uid 1000 — это 1000:

    nsenter -U --preserve-credentials -t $(cat $XDG_RUNTIME_DIR/dockerd-rootless/child_pid) \
        chown -R 1000:0 /mnt/store/ss/docker/compose/boba/compose/studio/app_root

Эта же команда нужна после `make -C build dev APP=studio`: цель заново
наполняет `app_root` файлами владельца `ss`, а studio должен в него писать.
Каталоги `data/krb/cache` закрыты от `ss` (режим `0700`), поиск IDE по ним
выдаёт «Permission denied» — это безвредно.

## Разработка и сборка

Рабочее дерево `/mnt/store/ss/docker/compose/boba` принадлежит `ss`: git,
правка кода и `.venv` работают без дополнительных прав. Окружение готовит
`source dev.sh` (внутри `uv sync`; пакеты идут из pypi). Дерево отладки и
compose собирает `make -C build dev APP=<приложение>` — docker ему не нужен.

Образы (`make -C build base`, `sandbox`, `plugin-rootfs-all`, `web`, `build`)
под `ss` сейчас не соберутся: в демоне `ss` нет базовых образов
`dmp/python:3.11-dist`, `dmp/glibc:2.28`, `dmp/gcc:8.5.0`, `dmp/nodejs:20`,
`dmp/nodejs:22`. Их нужно один раз загрузить в демон `ss`
(`docker load` из архива `docker save`), после этого сборка идёт как раньше.

## Отладка из IDE

VS Code запускает отладку от того пользователя, под которым открыто
Remote-SSH соединение, поэтому подключаться нужно сразу как `ss`.

Отлаживаемый процесс работает на хосте и тоже создаёт cgroup на каждый вызов
песочницы. Для него есть отдельный экземпляр юнита —
`boba-sandbox@debug.service`, а в `launch.json` у конфигураций с
`BOBA_CGROUP_BASE` интерпретатором указан `.vscode/python-debug-slice.sh`.
Скрипт запускает юнит, если он не запущен, и стартует python из `.venv` внутри
`boba-debug.slice` (`systemd-run --user --scope`): без этого процесс остаётся в
cgroup SSH-сессии, перенос в делегированное поддерево ядро запрещает, и старт
падает с `CgroupError`. В терминале то же самое:

    .vscode/python-debug-slice.sh -m boba.mcp_server --config <base>/conf/config.toml

`BOBA_CGROUP_BASE` для отладки:
`/sys/fs/cgroup/user.slice/user-10003.slice/user@10003.service/boba.slice/boba-debug.slice/boba-sandbox@debug.service/sandbox`.

Конфигурации `launch.json` берут `compose/<приложение>/conf` — тот же конфиг
и тот же keytab, что у контейнера; keytab `ss` читает через группу, поэтому
вход в Postgres по Kerberos в отладке работает. Тесты с песочницей берут
каталог cgroup из той же переменной `BOBA_CGROUP_BASE` и без неё либо вне
`boba-debug.slice` пропускаются с объяснением причины.

Отладочный процесс и контейнер одного приложения делят `data/`; файлы,
созданные одним, второй может не суметь перезаписать — владельца выравнивает
команда `nsenter ... chown` из раздела о данных.
