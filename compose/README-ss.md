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
- `app_root/` — владелец `ss`, только чтение для контейнера; его
  перекладывает `make -C build dev APP=<приложение>`.

Модели и образы песочницы в `compose/<приложение>` не лежат: контейнеры
монтируют их из общего каталога `runtime/` (следующий раздел).

## Каталог runtime/: зависимости, которые подкладываются для работы

`runtime/` в корне репозитория — то, что приложению нужно для работы, но не
является ни кодом, ни данными. Экземпляр один на репозиторий: его монтирует
compose, читает отладка на хосте и берёт в контекст сборка образа. В git
каталог не попадает.

| Путь | Что лежит | Кто кладёт | Кто читает |
|---|---|---|---|
| `runtime/models/` | `fastembed`, `rapidocr`, `onnx-genai` | `make -C build fetch` | контейнеры (том `/app/boba/models`), отладка |
| `runtime/third/` | `bin/bwrap`, `bin/fuse2fs`, `lib/libstdc++`, `lib/libgcc_s` | `make -C build sandbox` | отладка и тесты на хосте |
| `runtime/sandbox/` | шаблон `workspace.ext4`, `plugins/<пакет>/rootfs.ext4`, `tools/` | `make -C build sandbox`, `plugin-rootfs-all` | studio (том `/app/boba/sandbox`), сборка образа mcp, отладка |

`runtime/third` контейнеры не монтируют: в образе свой `/app/boba/third` — там
лежит и интерпретатор python. Владелец всего каталога — `ss`, запись есть
только у него; остальным — чтение, поэтому пользователь контейнера (на хосте
625287) файлы читает. Запрет записи группе и прочим в `third/bin` обязателен:
песочница отказывается запускать `bwrap` и `fuse2fs` из каталога, куда может
писать кто-то ещё (`UntrustedBinaryError ... is group-writable`).

Файлы, созданные контейнером, `ss` читает, но изменить или удалить напрямую
может не все (у новых файлов нет записи для группы). Сменить владельца или
права без sudo можно из user namespace демона, где `ss` — это root, а
контейнерный uid 1000 — это 1000:

    nsenter -U --preserve-credentials -t $(cat $XDG_RUNTIME_DIR/dockerd-rootless/child_pid) \
        chown -R 1000:0 /mnt/store/ss/docker/compose/boba/compose/studio/app_root

Каталоги `data/krb/cache` закрыты от `ss` (режим `0700`), поиск IDE по ним
выдаёт «Permission denied» — это безвредно.

## Разработка и сборка

Рабочее дерево `/mnt/store/ss/docker/compose/boba` принадлежит `ss`: git,
правка кода и `.venv` работают без дополнительных прав. Окружение готовит
`source dev.sh` (внутри `uv sync`; пакеты идут из pypi). Дерево compose
собирает `make -C build dev APP=<приложение>`, дерево отладки —
`make -C build debug APP=<приложение>`; docker нужен им только для сборки
фронта, когда его исходники новее готовой сборки. Зависимости в `runtime/`
кладут `make -C build fetch`, `sandbox` и `plugin-rootfs-all`.

Образы (`make -C build base`, `sandbox`, `plugin-rootfs-all`, `web`, `build`)
под `ss` сейчас не соберутся: в демоне `ss` нет базовых образов
`dmp/python:3.11-dist`, `dmp/glibc:2.28`, `dmp/gcc:8.5.0`, `dmp/nodejs:20`,
`dmp/nodejs:22`. Их нужно один раз загрузить в демон `ss`
(`docker load` из архива `docker save`), после этого сборка идёт как раньше.

## Отладка и тесты на хосте: каталог debug/

Процесс, запущенный на хосте (отладка из IDE, тесты), работает от `ss`, а
контейнер compose — от 625287. Если оба пишут в один каталог, каждый
натыкается на файлы другого: кэш билетов Kerberos с чужим владельцем, журнал
вызова с режимом `0600`, который второй не прочтёт. Поэтому у отладки своё
дерево — `debug/<приложение>` в корне репозитория. В git оно не попадает, всё
в нём принадлежит `ss`, и в нём только настоящие каталоги и файлы:

| Путь в `debug/<приложение>/` | Что это |
|---|---|
| `conf/config.toml` | собственный конфиг отладки, режим `0600` |
| `conf/plugins/`, `conf/stand.toml` | копии настроек плагинов и стенда, режим `0600` |
| `data/` | всё, что процесс пишет: кэши билетов `krb/`, образы `workspace/*.ext4`, журналы `tool-logs/`, `dump/`, вложения chainlit `files/` |

Дерево создаёт `make -C build debug APP=<приложение>` (без `APP` — все три).
Конфиг отладки — копия `compose/<приложение>/conf/config.toml`, в которой
отладочные значения прописаны прямо в секции `[env]`; вместе с ним копируются
и секреты, поэтому файлы закрыты от всех, кроме `ss`. Готовый конфиг цель не
перезаписывает: чтобы пересоздать, удалите `debug/<приложение>/conf`.
Изменения конфига compose в конфиг отладки сами не попадают.

Что в `[env]` конфига отладки отличается от compose:

| Ключ | Значение в отладке |
|---|---|
| `port`, `url_prefix` | chainlit — `8601`, `/boba-debug`; studio — `8602`, `/boba-studio-debug`; mcp — `8651`, `public_url = "http://localhost:8651"` |
| `instance_id`, `messaging_provider` | `"dev"`, `"local"` |
| `tool_launcher` | `"process"` |
| `cgroup_base` | каталог `sandbox` юнита `boba-sandbox@debug.service` |
| `models`, `third`, `sandbox` | `${env.base}/../../runtime/models`, `.../runtime/third`, `.../runtime/sandbox` |
| `krb` | `${env.base}/../../compose/<приложение>/conf/krb` |
| `app_root` | chainlit — `${env.base}/../../packages/apps/boba-chainlit/assets`; studio — `.../packages/apps/boba-studio/assets` |
| `workflow_page` (studio) | `"http://127.0.0.1:5173"` — vite dev-сервер |

`${env.base}` — каталог `debug/<приложение>`: загрузчик выводит его из
расположения конфига. `data` остаётся `${env.base}/data`, то есть
`debug/<приложение>/data`.

Keytab и `krb5.conf` в отладку не копируются: ключ `krb` указывает на
`compose/<приложение>/conf/krb`. Keytab один на приложение, `ss` читает его
через группу; вторая копия ключа сервиса на диске ничего бы не дала, а при
смене пароля пришлось бы менять обе.

### Статика и вложения chainlit

`app_root` в отладке — прямо каталог ассетов пакета
`packages/apps/boba-chainlit/assets`: правка ассета видна без пересборки.
Писать в него приложение не должно. Сам chainlit держит вложения сессий в
`<app_root>/.files` и создаёт этот каталог при импорте; настройки для него в
chainlit нет. Поэтому каталог вложений задаёт boba — обязательным ключом
`[chainlit].files_dir` (в конфигах `"${env.data}/files"`): точка входа
подменяет путь внутри chainlit до того, как тот успеет создать `.files`.
Ключ одинаков для compose и отладки; у контейнера вложения ложатся в том
`data`.

У studio `app_root` — `packages/apps/boba-studio/assets`, сборка страницы
workflow лежит в нём как `workflow/`, и конфиг отладки называет её явно:
`dist = "${env.app_root}/workflow"`. Studio в `app_root` ничего не пишет.

### Запуск из IDE

VS Code запускает отладку от того пользователя, под которым открыто
Remote-SSH соединение, поэтому подключаться нужно сразу как `ss`.
Конфигурации `launch.json` для chainlit, studio, `tool` и pytest задают только
то, чего нет в конфиге: рабочий каталог `debug/<приложение>`, путь
`--config debug/<приложение>/conf/config.toml` и `PATH` с `LD_LIBRARY_PATH` на
`runtime/third`. Перед стартом chainlit задача `chainlit: debug tree`
выполняет `make -C build debug APP=chainlit`.

В терминале то же самое (пример — boba-mcp):

    cd debug/mcp
    PATH=$PWD/../../runtime/third/bin:$PATH LD_LIBRARY_PATH=$PWD/../../runtime/third/lib \
        ../../.venv/bin/python -m boba.mcp_server --config $PWD/conf/config.toml

### Режим песочницы в отладке

По умолчанию в конфиге отладки стоит `tool_launcher = "process"`: инструмент
исполняется обычным процессом, без bwrap и cgroup. Режим `sandbox` в отладке
тоже свой и compose не задевает: образы пользователей лежат в
`debug/<приложение>/data/workspace`, а cgroup на каждый вызов создаётся в
отдельном экземпляре юнита — `boba-sandbox@debug.service` (его каталог уже
записан в `cgroup_base` конфига отладки).

Включается он значением `tool_launcher = "sandbox"` в конфиге отладки либо
переменной `BOBA_TOOL_LAUNCHER=sandbox`, а интерпретатором служит
`.vscode/python-debug-slice.sh` вместо `../../.venv/bin/python`. Скрипт
запускает юнит, если он не запущен, и стартует python из `.venv` внутри
`boba-debug.slice` (`systemd-run --user --scope`): без этого процесс остаётся
в cgroup SSH-сессии, перенос в делегированное поддерево ядро запрещает, и
старт падает с `CgroupError`.

### Тесты

Тесты запускаются из `debug/chainlit` с тем же окружением, что у конфигураций
pytest в `launch.json`:

    cd debug/chainlit
    BOBA_CONFIG_PATH=$PWD/conf/config.toml ../../.venv/bin/pytest ../../packages/apps/boba-mcp/tests

Тесты, которые сами поднимают приложение, берут конфиг его дерева отладки
(`debug/studio`, `debug/mcp`), зависимости — из `runtime/`, а данные кладут в
каталог прогона pytest либо в `debug/<приложение>/data`; кэш pytest лежит в
`debug/.pytest_cache`. От `compose/<приложение>` тесты не зависят. Тесты с
песочницей берут каталог cgroup из `BOBA_CGROUP_BASE` и без неё либо вне
`boba-debug.slice` пропускаются с объяснением причины.

Отладочный chainlit и e2e-тесты ходят к сервису boba-mcp по сети — к
контейнеру compose. Это не общий каталог: файлы пользователя и журналы
вызовов пишет сам контейнер у себя в `compose/mcp/data`.
