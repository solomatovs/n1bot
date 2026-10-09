# boba пользователя alexeyk500 (ветка dev)

Это отдельная установка boba: свой рабочий каталог git, своя сборка, свои
контейнеры в собственном rootless-docker, своя база и свои маршруты nginx.
Всё делается под пользователем `alexeyk500`, sudo не нужен. Документ описывает,
где что лежит, как собирать и запускать, что уже настроено и чего пока не хватает.

## 1. Что где лежит

| Что | Путь |
|---|---|
| Рабочий каталог git, ветка `dev` | `/mnt/store/alexeyk500/docker/compose/boba` |
| Сборка чата и studio | `build/chainlit`, `build/studio` (у каждого свой `Makefile` и `Dockerfile`) |
| Скачанное для сборки образов | `build/<app>/src/` (исходники bwrap/fuse/e2fsprogs, uv, фронтовые библиотеки, колесо oracledb; в git не попадает) |
| Общие runtime-зависимости | `runtime/` — модели, `third`, образы песочницы; один экземпляр на оба приложения, на compose и на отладку (раздел 1.1) |
| Дерево отладки на хосте | `debug/<app>/` — свой конфиг и свои данные процесса отладки (раздел 7) |
| Стек чата | `compose/chainlit` (`docker-compose.yml`, `.env`, `conf/`, `data/`, `app_root/`) |
| Стек studio | `compose/studio` |
| Конфиг приложения | `compose/<app>/conf/config.toml`, плагины — `conf/plugins/*.toml` |
| Окружение контейнера | `compose/<app>/conf/boba.env` |
| Kerberos | `compose/<app>/conf/krb/krb5.conf`, `boba-svc.keytab` (кладёт администратор) |
| Профиль seccomp контейнера | `compose/<app>/conf/seccomp-sandbox.json` |
| Данные приложения | `compose/<app>/data` (владелец — пользователь контейнера) |
| Данные docker | `/mnt/store/alexeyk500/docker/data` |
| Юнит поддерева cgroup песочницы | `~/.config/systemd/user/boba-sandbox@.service` |
| Отладка в VS Code | `.vscode/launch.json`, `.vscode/tasks.json`, `.vscode/python-debug-slice.sh` |

В ветке `dev` приложений два: `chainlit` (чат, порт 8501) и `studio` (API и
страницы, порт 8502). Отдельного сервиса `boba-mcp` в `dev` нет: инструменты и
песочница работают внутри chainlit и studio. У администратора (ветка
`feature/dag-runner-only`) инструменты вынесены в `boba-mcp`, сборка одна на все
приложения (`build/Makefile`), пакеты приложений лежат в `packages/apps`. Здесь —
`build/chainlit`, `build/studio` и `packages/agents`.

### 1.1. Каталоги `runtime/` и `debug/`

Контейнеры compose и процессы отладки на хосте работают одновременно, поэтому
всё, что они читают, лежит в одном общем месте, а всё, что пишут, — у каждого
в своём. Символических и жёстких ссылок в раскладке нет: на общий каталог
указывают явные пути в `docker-compose.yml` и в конфиге отладки.

```
runtime/                      только чтение; в git не попадает
  models/
    fastembed/                веса эмбеддера (chainlit и studio)
    rapidocr/                 модели OCR (chainlit и studio)
    onnx-genai/               модели переформулировщика (только chainlit)
  third/
    bin/                      bwrap, fuse2fs
    lib/                      libstdc++, libgcc_s для колёс
  sandbox/
    workspace.ext4            шаблон рабочего каталога песочницы
    plugins/<пакет>/rootfs.ext4   образы корня плагинов, общие для приложений
    tools/                    mke2fs, которым make собирает образы

debug/                        в git не попадает
  chainlit/conf/              config.toml и plugins/ процесса отладки чата
  chainlit/data/              всё, что процесс отладки чата пишет
  studio/conf/, studio/data/  то же для studio
  .pytest_cache/              кэш pytest
```

Кто что берёт:

| Потребитель | Модели | Песочница | `third` | Статика (`app_root`) | Данные |
|---|---|---|---|---|---|
| контейнер compose | том `runtime/models` | том `runtime/sandbox` | свой, внутри образа | chainlit — из образа, studio — том `compose/studio/app_root` | `compose/<app>/data` |
| отладка на хосте | `runtime/models` | `runtime/sandbox` | `runtime/third` | ассеты пакета: `packages/agents/boba-<app>/assets` | `debug/<app>/data` |

Права на `runtime/`: владелец — ты, остальным только чтение (`o+rX`), записи
для группы и прочих нет. Пользователь контейнера (`559751` на хосте) читает
каталог как «прочий». Каталог с `bwrap`, открытый на запись группе, песочница
отвергает ошибкой `UntrustedBinaryError`; цель `sandbox` права выставляет сама.

Каталоги `compose/<app>/models` и `compose/<app>/third`, а также
`build/<app>/src/sandbox` больше не существуют — их содержимое теперь в
`runtime/`.

## 2. Сборка

Сборка идёт штатными целями `make` в твоём docker, без дополнительных скриптов.
У каждого приложения свой каталог сборки: `build/chainlit` и `build/studio`.
Список целей и переменных печатает `make -C build/chainlit help`.

Нужны базовые образы администратора; они уже загружены в твой docker:

```
docker images | grep '^dmp/'
dmp/glibc:2.28   dmp/gcc:8.5.0   dmp/python:3.11-dist   dmp/nodejs:20 (chainlit)   dmp/nodejs:22 (studio)
```

### Порядок с нуля

Сборка долгая, поэтому запускается в tmux с выводом в файл: обрыв соединения
её не прервёт. Сначала chainlit, затем studio; две сборки одновременно не
запускай.

```
cd /mnt/store/alexeyk500/docker/compose/boba

make -C build/chainlit fetch          # коротко, можно без tmux
source dev.sh                         # .venv через uv sync; нужен цели web у studio

tmux new-session -d -s build-chainlit \
    'make -C build/chainlit sandbox plugin-rootfs-all web build dev > /tmp/build-chainlit.log 2>&1'
tail -f /tmp/build-chainlit.log
tmux has-session -t build-chainlit    # идёт ли ещё

tmux new-session -d -s build-studio \
    'make -C build/studio fetch web build dev > /tmp/build-studio.log 2>&1'
```

Цели `sandbox` и `plugin-rootfs-all` пишут в общий `runtime/`, поэтому их
достаточно выполнить один раз, у chainlit; у studio повторять не нужно.

То же одной целью: `make -C build/chainlit all` и `make -C build/studio all`
(`all` = `fetch sandbox plugin-rootfs-all web build dev`). Перед первым `all`
у studio должен существовать `.venv`.

| Цель | Что делает | chainlit | studio |
|---|---|---|---|
| `fetch` | качает в `build/<app>/src` исходники bwrap, fuse, e2fsprogs, uv, фронтовые библиотеки, собирает колесо python-oracledb; модели OCR, веса эмбеддера и модели onnx (только chainlit) кладёт в `runtime/models` | 5 мин с пустым кэшем, 1 мин с готовым | 1 мин, 20 с с готовым кэшем |
| `sandbox` | bwrap, fuse2fs и библиотеки в `runtime/third`, шаблон `workspace.ext4` в `runtime/sandbox` | 30 с | не нужна |
| `plugin-rootfs-all` | образы корня песочницы для 13 плагинов в `runtime/sandbox/plugins` | 10 мин при квоте 8 ядер | не нужна |
| `web` | фронт: `page.js` и UI chainlit; у studio — `openapi.json` и страница workflow | 1 мин | 20 с |
| `build` | образ `boba-<app>:<версия>` | 3 мин | 1,5 мин |
| `dev` | готовит дерево compose: каталоги `data/`, `app_root`, `conf/boba.env`; модели и песочницу не копирует — compose монтирует их из `runtime/` | секунды | секунды |
| `debug` | готовит дерево отладки `debug/<app>`: конфиг и каталоги данных (раздел 7); готовый конфиг не перезаписывает | секунды | секунды |

Время — на этом хосте; studio быстрее, потому что берёт слои из кэша сборки
chainlit. Смена числа ядер квоты меняет `JOBS`, и следующая сборка идёт без
кэша docker.

После `build` и `dev` пересоздай контейнер (раздел 3): образ и `app_root`
новые. Порядок важен, когда правка кода требует нового ключа в `config.toml`:
сначала `build`, затем ключ в конфиге, затем `docker compose up -d`.

Образы корня плагинов compose монтирует томом из `runtime/sandbox`, а
`plugin-rootfs` заменяет файл образа на месте. Работающий контейнер держит
открытым прежний файл; после пересборки плагинов пересоздай контейнеры.

### Что сборка определяет сама

- **Rootless-docker.** Контейнеры сборки пишут в каталоги репозитория. В
  rootless-docker владельцу каталогов соответствует root контейнера, в обычном
  docker — твой uid. `make` узнаёт режим по `docker info` и подставляет нужного
  пользователя сам (`RUN_OWNER` в `build/common.mk`).
- **Число заданий.** Переменная `JOBS` — сколько заданий запускает один шаг
  компиляции. По умолчанию это квота CPU твоей учётной записи (сейчас 4 ядра);
  `nproc` показал бы все 32 ядра хоста. Задать вручную: `make ... JOBS=2`.
  Docker ведёт несколько стадий образа сразу, поэтому на стадии `build` средняя
  нагрузка хоста на несколько минут поднимается выше числа ядер квоты.
  Параллельность самого `make` (`-j`) не добавляй.
- **Версии качалки моделей.** `fetch` ставит `fastembed` и `huggingface_hub`
  тех версий, что записаны в `uv.lock` (файл `build/<app>/src/constraints.txt`
  делается из него автоматически). Кэш Hugging Face в
  `runtime/models/fastembed` устроен самой библиотекой: файлы в `snapshots/` —
  символические ссылки на `blobs/`. Это её штатная раскладка, не трогай.
- **Python.** Скрипты сборки запускаются python из `.venv`; пока `.venv` нет —
  `python3.11` из `PATH`. Другой интерпретатор: `make ... PYTHON=<путь>`.
- **Порт и префикс адреса.** `dev` пишет `compose/<app>/conf/boba.env` заново,
  а `BOBA_PORT` и `BOBA_URL_PREFIX` берёт из секции `[env]` твоего
  `compose/<app>/conf/config.toml` (`port`, `url_prefix`). Префикс меняется
  только там; `boba.env` руками править не нужно.

`uv sync` (его зовёт `dev.sh`) файл `uv.lock` не меняет.

Пустой или битый `packages/agents/boba-studio/web/workflow/openapi.json`
(остаётся, если `web` у studio запускали без `.venv`) ломает эту стадию ошибкой
`Cannot read properties of undefined (reading 'openapi')` — удали файл и
повтори `make -C build/studio web`.

## 3. Запуск и остановка

```
cd /mnt/store/alexeyk500/docker/compose/boba/compose/chainlit
docker compose config -q      # проверка файла
docker compose up -d chainlit # второй узел chainlit2 поднимается так же, по имени
docker compose logs -f chainlit
docker compose down

cd ../studio && docker compose up -d studio
```

Тома контейнера: `./conf` (чтение), `./data` (запись), `../../runtime/models`
и `../../runtime/sandbox` (чтение), каталог cgroup песочницы; у studio ещё
`./app_root`. Вложения сессий чата chainlit пишет в
`compose/chainlit/data/chainlit-files` (ключ `[chainlit].files_dir`), а не в
`app_root`.

Перед запуском должны работать юниты поддерева cgroup (раздел 5), иначе compose
не найдёт каталог из `BOBA_SANDBOX_CGROUP`.

Адреса контейнеров в сети `alexeyk500` закреплены: chainlit `10.120.0.6`,
chainlit2 `10.120.0.36`, studio `10.120.0.12`. Имена и порты менять не стоит: на
них настроены nginx и межпользовательские связи.

## 4. Адреса

| Адрес | Куда ведёт |
|---|---|
| `https://loshara.com/alexeyk500/boba/` | контейнер `chainlit:8501` |
| `https://loshara.com/alexeyk500/boba-studio/` | контейнер `studio:8502`; сам корень отвечает `404`, API — `/alexeyk500/boba-studio/api/openapi.json` |
| `https://loshara.com/alexeyk500/boba-debug/` | процесс отладки чата на хосте, порт `8611` |
| `https://loshara.com/alexeyk500/boba-studio-debug/` | процесс отладки studio на хосте, порт `8612` |
| `https://loshara.com/alexeyk500/boba-search/` | стенд поиска на хосте, порт `8710` |
| `https://loshara.com/alexeyk500/boba-mcp/` | заготовка под `boba-mcp:8650`; в `dev` сервиса нет |

Пока контейнер или процесс не запущен, nginx отвечает `502`. Порты `8611`,
`8612`, `8710` открыты в файрволе хоста только для nginx; процесс отладки должен
слушать `0.0.0.0` (в конфиге так и стоит: `[chainlit].host`, `[studio].host`).

Cookie входа называется `access_token_alexeyk500`, а не `access_token`: твоя
установка и установка администратора живут на одном домене `loshara.com`, с
одинаковым именем они затирали бы вход друг друга.

## 5. Песочница, cgroup и защита контейнера

Контейнеры запущены без root и без capabilities:

- `user: 1000:1000` (значения в `.env`: `BOBA_UID`, `BOBA_GID`). В rootless-docker
  это не uid хоста, а uid внутри пространства имён; на хосте он виден как `559751`.
  Поэтому `data/` и keytab принадлежат `559751`;
- `cap_drop: ALL`, `no-new-privileges`, профиль `seccomp-sandbox.json`,
  `systempaths=unconfined` и `/dev/fuse` (нужны песочнице инструментов: она
  монтирует образы через fuse в своём user namespace);
- лимиты: 16 ГБ памяти, 8 CPU, 2048 процессов;
- `cgroup_parent: boba-<app>.slice`.

Каждый запуск инструмента приложение переносит в собственную cgroup. Поддерево
для этого даёт пользовательский юнит systemd, по экземпляру на приложение:

```
systemctl --user status boba-sandbox@chainlit boba-sandbox@studio boba-sandbox@debug
```

Каталог поддерева записан в `compose/<app>/.env` (`BOBA_SANDBOX_CGROUP`) и
монтируется в контейнер как `/cgroup/boba-sandbox-<app>`; это же значение стоит
в `[env].cgroup_base` конфига.

**Не перезапускай `boba-sandbox@<app>.service`, пока работает контейнер этого
приложения.** Рестарт пересоздаёт каталог cgroup, а контейнер продолжает держать
старый, уже удалённый: инструменты начнут падать. Порядок: `docker compose down`,
затем `systemctl --user restart boba-sandbox@<app>`, затем `docker compose up -d`.

## 6. Конфигурация и учётные данные

`config.toml` обоих приложений собран по структуре конфига администратора; его
секреты сюда не переносились.

Сгенерировано для этой установки (одинаково в chainlit и studio, менять только
вместе): `[site].auth_secret`, `[site].proxy_secret`,
`[site].database_encryption_key`, пароль входа `admin` в
`[site.local_auth.users]`.

Сервисная учётная запись у обоих стендов одна — `boba-svc@LOSHARA.COM`, та же,
что у администратора. Ей принадлежит SPN `HTTP/loshara.com` (вход по Kerberos)
и право делегирования в postgres-17, ClickHouse и Confluence. Твоя база —
`boba_alexeyk500` на `postgres-17.loshara.com`, владелец — роль `boba-svc`, вход
по Kerberos (`[site].pg_auth_method = "kerberos"`). Расширения те же, что в базе
администратора: `age`, `btree_gin`, `pg_trgm`, `unaccent`, `vector`.

Учётные данные положил администратор: keytab и значения в `config.toml`
(пароль привязки к LDAP, токен LLM). Не копируй их в другие места и не
коммить.

Файл keytab (в обоих приложениях один и тот же):

| Куда | Владелец и права |
|---|---|
| `compose/chainlit/conf/krb/boba-svc.keytab` | `10002:559751`, `0440` |
| `compose/studio/conf/krb/boba-svc.keytab` | `10002:559751`, `0440` |

`10002` — это ты (читает процесс отладки на хосте), `559751` — пользователь
контейнера (uid 1000 внутри rootless-docker). На этот файл ссылаются
`[site].krb_http_keytab` и `[site].krb_pg_keytab`.

Поля, которые заполняет администратор (в `config.toml` уже заполнены, в
остальных файлах стоит `CHANGE_ME`):

| Поле | Файлы | Что это |
|---|---|---|
| `[site].ldap_bind_password` | `compose/chainlit/conf/config.toml`, `compose/studio/conf/config.toml` | пароль учётной записи `LOSHARA\readonly`, которой приложение читает группы пользователей в AD |
| `[site.llm.requesty].api_key` | те же | токен LLM-провайдера; без него чат не отвечает |
| `[site.llm.local].api_key` | те же | ключ локального LLM-сервера, нужен только при `llm_provider = "local"` |
| `password` | `conf/plugins/mail.toml` обоих приложений | SMTP; плагин выключен (`enable = false`) |
| `[site].llm_token` | `compose/ix-llm-describer/conf.toml` | токен LLM для индексатора описаний |
| `token` в `[[ix.cfl_indexer.sources]]` | `compose/cfl-indexer/conf.toml` | токен Confluence |
| `password` в `[[ix.meta_scraper.sources]]` | `compose/pg-meta-scraper/conf.toml`, `compose/ora-meta-scraper/conf.toml` | пароли стендовых баз |

Файлы `compose/ix-*`, `compose/*-meta-scraper`, `compose/cfl-indexer` — конфиги
индексаторов. Они смотрят в базу администратора (`pg_database = "boba"`); перед
запуском у себя поменяй базу на `boba_alexeyk500`, иначе индексаторы будут
писать в его данные.

### Общая учётная запись: чем это грозит

`boba-svc` — владелец и базы администратора `boba`, и твоей. Твоё приложение
технически может подключиться к его базе. Имя базы задаёт только
`[site].pg_database`; не меняй его на `boba`.

### Тестовые базы

Тесты ветки `dev` создают базы с жёстко заданными именами: `boba_test…`
(`packages/testing/boba-stand/src/boba/stand/database.py`, шаблон
`boba_stand_template`) и `boba_ui_test` (`tests/ui/conftest.py` у chainlit и
studio). Настройки префикса в `dev` нет. С общей учётной записью эти базы тебе
доступны, но они те же самые, что у администратора: тесты создают и сносят в
них схемы. Если оба стенда гоняют тесты одновременно, прогоны ломают друг другу
данные и дают ложные падения. Перед прогоном договорись с администратором; код
тестов под это не менялся.

## 7. Разработка

```
cd /mnt/store/alexeyk500/docker/compose/boba
source dev.sh        # создаёт .venv (uv sync) и активирует его
```

`dev.sh` требует колесо `python-oracledb` в `build/chainlit/src/oracledb`. Его
собирает стадия `fetch` chainlit (раздел 2); после полной сборки оно на месте.

`uv sync` перезаписывает в `uv.lock` абсолютный путь к этому колесу
(`/app/docker/compose/boba/...` на путь твоего дерева). В `git status` появится
`M uv.lock` — это изменение коммитить не нужно.

### Отладка на хосте

У процесса отладки свой конфиг и свои данные — `debug/<app>/conf` и
`debug/<app>/data`. С контейнером compose он делит только то, что читает:
`runtime/`, ассеты в `packages` и `conf/krb` дерева compose (keytab и
`krb5.conf` не копируются, конфиг отладки ссылается на них путём). База
данных у отладки и контейнера одна — `boba_alexeyk500`.

Дерево отладки создаёт цель `debug`; порт и префикс твоей учётной записи
передаются переменными:

```
make -C build/chainlit debug DEBUG_PORT=8611 DEBUG_PREFIX=/alexeyk500/boba-debug
make -C build/studio   debug DEBUG_PORT=8612 DEBUG_PREFIX=/alexeyk500/boba-studio-debug
```

Цель берёт `compose/<app>/conf/config.toml` и `conf/plugins`, прописывает в
секции `[env]` отладочные значения и кладёт результат в `debug/<app>/conf`
(права `0600`: в файле секреты). Что отличается от конфига compose:

| Ключ `[env]` | compose | отладка |
|---|---|---|
| `port`, `url_prefix` | `8501` `/alexeyk500/boba`; `8502` `/alexeyk500/boba-studio` | `8611` `/alexeyk500/boba-debug`; `8612` `/alexeyk500/boba-studio-debug` |
| `instance_id` | `alexeyk500-node1`, `alexeyk500-studio` | `dev` |
| `messaging_provider` | `postgres` | `local` |
| `tool_launcher` | `sandbox` | `process` |
| `cgroup_base` | `/cgroup/boba-sandbox-<app>` | каталог юнита `boba-sandbox@debug.service` |
| `models`, `sandbox`, `third` | `${env.base}/...` (внутри контейнера) | `${env.base}/../../runtime/...` |
| `app_root` | `${env.base}/app_root` | `${env.base}/../../packages/agents/boba-<app>/assets` |
| `krb` | `${env.base}/conf/krb` | `${env.base}/../../compose/<app>/conf/krb` |
| `data` | `${env.base}/data` | то же выражение; `base` — это `debug/<app>` |

У studio дополнительно `[studio].dist = "${env.app_root}/workflow"`: сборка
страницы лежит в ассетах пакета без `public/`.

Готовый `debug/<app>/conf/config.toml` цель не перезаписывает. Если ты поменял
конфиг compose и хочешь те же правки в отладке — внеси их в оба файла либо
удали конфиг отладки и повтори `make ... debug`. Режим песочницы в отладке
включается правкой `tool_launcher = "sandbox"` в `debug/<app>/conf/config.toml`.

Запуск из VS Code: конфигурации `Chainlit: agent` и `Studio: api…` в
`.vscode/launch.json`. Рабочий каталог — `debug/<app>`, конфиг —
`debug/<app>/conf/config.toml`, в окружении только `PATH` и `LD_LIBRARY_PATH`
с `runtime/third`; порт, префикс и режим запуска инструментов заданы в
конфиге, а не переменными. Python запускается через
`.vscode/python-debug-slice.sh`: процесс попадает в срез `boba-debug.slice` и
получает поддерево cgroup `boba-sandbox@debug.service`. Перед запуском чата
VS Code выполняет задачу `chainlit: debug tree` (та же цель `debug`).

То же из терминала:

```
cd /mnt/store/alexeyk500/docker/compose/boba
export PATH="$PWD/.venv/bin:$PWD/runtime/third/bin:$PATH" LD_LIBRARY_PATH="$PWD/runtime/third/lib"
(cd debug/chainlit && ../../.vscode/python-debug-slice.sh ../../packages/agents/boba-chainlit/src/boba/chainlit/main.py --config "$PWD/conf/config.toml")
(cd debug/studio   && ../../.vscode/python-debug-slice.sh -m boba.studio --config "$PWD/conf/config.toml")
```

Чат отдаёт статику прямо из `packages/agents/boba-chainlit/assets` и ничего
туда не пишет: вложения сессий идут в `debug/chainlit/data/chainlit-files`.
Правка ассета видна без пересборки. Собранные `page.js`, `ui/` и
`assets/workflow` у studio кладёт туда цель `web`.

Тесты берут конфиги из `debug/<app>/conf`, а образы песочницы и модели — из
`runtime/`; кэш pytest лежит в `debug/.pytest_cache`.

## 8. Сеть: что разрешено и о чём просить

Контейнеры разных пользователей изолированы. Твоим контейнерам открыто только:

- `postgres-17:5432`; домен `samba-ad` (88, 389, 464, 636, 3268, 3269); `nginx:80,443`;
- базы и стенды инструментов: `clickhouse-01:8123`, `oracle:1521`,
  `edge-pg-*:5432`, `edge-gp-6/7:5432`, `ix-test:5432`, `edge-ch-*:8123`,
  `edge-chs-*:8123`, `edge-ora-18/21/23:1521`.

В обратную сторону nginx видит только контейнеры `chainlit:8501`, `studio:8502`
и `boba-mcp:8650` (по именам, поэтому имена контейнеров менять нельзя).
Всё остальное (например `redis`, `postgres-18`, `grafana`) закрыто. Если нужна
новая связь — попроси администратора добавить строку в
`/etc/docker-netmap/cross.conf`, указав контейнер и порт.

## 9. Чего не делать

- не перезапускать `boba-sandbox@<app>` под работающим контейнером (раздел 5);
- не менять `auth_secret`, `database_encryption_key` и имя cookie в одном
  приложении без другого: studio перестанет принимать вход чата, сохранённые
  секреты соединений перестанут расшифровываться;
- не публиковать порты контейнеров на хост (`127.0.0.1:8501:8501`), как было в
  `dev`: хост общий, наружу ходит nginx;
- не ставить `BOBA_UID`/`BOBA_GID` равными своему uid хоста (10002): внутри
  rootless-docker это другой пользователь, и поддерево cgroup, keytab и `data/`
  станут ему недоступны;
- не коммитить `compose/*/docker-compose.yml` с адресами `10.120.*` в общую
  ветку без договорённости: это настройки этого хоста.
