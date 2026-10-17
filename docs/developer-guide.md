# Руководство разработчика: tool-плагины, connection, песочница

Документ ведёт от первого вызова до собранного образа песочницы на одном
сквозном примере. Мы напишем плагин `redis`: сначала инструмент, который
ходит на сервер из конфига администратора, потом тип connection, чтобы у
каждого пользователя были свои сервера, потом инструмент с двумя connection
сразу и потоковый инструмент для конвейера.
Имена в примере вымышленные, но каждый шаг повторяет живой код репозитория,
и в конце шага названо, где этот код лежит.

Содержание:

1. Что происходит, когда модель вызывает инструмент
2. Плагин с конфигом администратора
3. Секрет в конфиге
4. Тип connection: свой connection у каждого пользователя
5. Инструмент с connection пользователя
6. Kerberos: что тип обязан уметь
7. Потоковый инструмент: порты
8. Песочница: изоляция и образ
9. Зависимости и импорты: граница хоста и песочницы
10. Запуск руками и отладка
11. Симптомы и причины
12. Отклонения от стандарта и договорённости

---

## 1. Что происходит, когда модель вызывает инструмент

Приложение, которое исполняет инструменты плагинов, — сервис boba-mcp — мы
дальше называем **хостом**. Он публикует инструменты по MCP. Модель с ними
напрямую не говорит: беседу с моделью ведёт процесс агента, который чат
запускает на каждую открытую вкладку треда. Агент получает инструменты от
чата и просит чат их исполнить, а чат зовёт boba-mcp от имени пользователя
(подробно — `docs/mcp-over-acp-plan.md`, главы 5 и 6). Кроме того, у
чата есть свой маленький хост в собственном процессе: инструменты
соединений `connection_list` и `connection_search` (раздел 5); плагинов
чат не загружает.

Когда на хост приходит вызов инструмента, хост не исполняет код
инструмента у себя: он собирает команду и запускает её в отдельном
процессе. Функция, которая в этом процессе реально работает, называется
**телом** инструмента.

Тело живёт в pip-пакете, который мы называем **плагином**. Плагин объявляет
себя entry point'ом группы `boba.tools`, и хост находит все установленные
плагины сам, без списка в коде. У каждого плагина есть короткое имя,
**секция**: `pg`, `doc`, `web`. По секции хост находит конфиг плагина в
файле `conf/plugins/<секция>.toml` и подшивает его к общему конфигу
приложения как таблицу `[tool.<секция>]`. Поэтому внутри файла плагина
работают те же интерполяции, что и в основном конфиге: `${env.models}`,
`${site.redis_password}`, `${postgres}`.

Как тело запускается, решает секция `[tool_launcher]` конфига приложения.
Есть два способа:

- `provider = "process"`: обычный субпроцесс `python -m <модуль>` на хосте.
  Используется в разработке и под отладчиком.
- `provider = "sandbox"`: тело исполняется внутри изолированного контейнера
  на bwrap, с собственным образом корня `rootfs.ext4` для каждого плагина.
  Так работает релиз. Чтобы не платить за старт python и импорт тяжёлых
  библиотек на каждом вызове, для каждой секции при старте приложения
  поднимается **зигота**: процесс внутри песочницы, который уже всё
  импортировал и ждёт. Вызов инструмента становится форком зиготы.

Теперь сам путь вызова. Модель попросила `pg_query(connection="analytics",
sql="select 1")`. По дороге к хосту чат заменил имя `analytics` профилем
этого соединения пользователя, запечатанным ключом хоста (раздел 5), а в
параметр `thread_id` поставил тред, из которого пришёл вызов. Хост:

1. проверяет роли пользователя, пишет журнал, ставит отмену по кнопке;
2. подкладывает в аргументы вызова то, чего модель не присылала:
   конфиг секции `[tool.pg]` и профиль connection — открытый запечатанный
   профиль от чата либо заданное соединение сервера, если модель назвала
   его имя, — с билетом kerberos для этого вызова. Таблицу соединений
   хост не читает: она у чата;
3. превращает аргументы в команду: то, что прислала модель, становится
   флагами argv, а подложенное хостом уезжает отдельным JSON по файловому
   дескриптору;
4. отдаёт команду launcher'у.

Тело делает обратное: разбирает argv и JSON обратно в типизированные
модели, зовёт функцию инструмента и пишет ответ **конвертом** в другой
дескриптор. Конверт бывает двух видов: `ReplyOk` с результатом и
`ReplyError` с видом отказа и текстом для пользователя.

Из этого пути следуют три правила, которые объясняют всё дальнейшее:

- Модель видит в схеме только те параметры, которые сама заполняет.
  Подложенные хостом поля из схемы вырезаны: модель их не видит и подделать
  не может.
- Всё, что может быть секретом, идёт JSON-каналом, а не argv: argv виден в
  `ps`, в журнале и в трейсбеке.
- Тело ничего не знает о пользователе, ролях и таблице connections. Оно
  получает готовые модели и работает с ними. Вся политика решена чатом и
  хостом до запуска.

---

## 2. Плагин с конфигом администратора

Начнём с инструмента `redis_scan`: он перебирает ключи по шаблону на
сервере, который задаёт администратор в конфиге. Пока без пользовательских
connection.

### Пакет

```
packages/tools/boba-tool-redis/
    pyproject.toml
    src/boba/tool/redis/
        __init__.py
        plugin.py      # манифест: entry point boba.tools
        tools.py       # конфиг, инструменты, TOOLS
```

Больше нигде пакет не объявляется: `members` корневого `pyproject.toml`
берёт `packages/tools/*` глобом, а сборка находит плагин по entry point
группы `boba.tools` (`scripts/plugin_rootfs.py list`): `make all` собирает
его образ песочницы, а сборка образа boba-mcp ставит пакет в окружение
сервиса. В образ чата плагины не ставятся.

### Параметры инструмента: модель заполняет одни, хост — остальные

Инструмент — это `async def` с декоратором `@tool` из
`boba.toolkit.facade`. Модель заполняет только аргументы с `Field(...)`:
они попадают в её схему и едут флагами argv. Всё остальное подкладывает
хост, и таких **предопределённых injected-параметров** ровно шесть видов.
Принцип один: параметр объявляется аннотацией, хост узнаёт его по типу или
маркеру и подставляет значение на каждом вызове; модель этих параметров
не видит и подделать не может. Объявить любой из них может любой
инструмент любого плагина — никакой регистрации, кроме самой аннотации.

| Параметр | Аннотация | Откуда значение | Кто подставляет |
|---|---|---|---|
| аргумент модели | `pattern: Annotated[str, Field(...)]` | LLM | — (флаг argv `--pattern "user:*"`) |
| конфиг секции | `cfg: Annotated[RedisToolConfig, Injected]` | таблица `[tool.<секция>]` по `SECTION` модели | `InjectedConfig` на загрузке |
| connection | `connection: Annotated[RedisConnection, UserConnection]` | запечатанный профиль соединения пользователя от чата или заданное соединение сервера | `SealedConnectionParam` (`boba-connection-broker`) на вызове |
| субъект вызова | `subject: Annotated[Subject, Injected]` | `CallContext`: id пользователя, логин, роли, профиль — из токена вызывающего | `CallContextValues` на вызове |
| область вызова | `scope: Annotated[Scope, Injected]` | `CallContext`: тред из параметра `thread_id` с пометкой `x-boba-thread`; без него — личный workspace пользователя | `CallContextValues` на вызове |
| корень workspace | `root: Annotated[WorkspaceRoot, Injected]` | профиль запуска: `/workspace` в песочнице, `workdir` в `process` | `CallContextValues` на вызове |
| порт данных | `out: Annotated[Outbound[...], Injected]` | канал кадров графа workflow | песочница на вызове (раздел 7) |

Все injected-значения, кроме портов, уезжают телу одним JSON по
дескриптору, ключ — имя параметра. Модели контекста живут в core:
`Subject` и `Scope` в `boba.identity.context`, `WorkspaceRoot` в
`boba.canvas.keys`; список типов, которые хост умеет подставлять, — это
`CallContextValues.MODELS` в `boba.toolrun.callvalues`. Новый вид
значения добавляется туда же, а не обвязкой в плагине.

Параметр `thread_id` хост сам ставит в схему каждого своего инструмента
(`CallFields.threaded` в `boba.toolrun.call_id`): необязательная строка с
пометкой `x-boba-thread`. Наш чат прячет его от модели и подставляет тред
вызова, чужой клиент MCP показывает модели, и она заполняет его сама или
работает в личном workspace. Телу этот параметр не уходит: по нему хост
выбирает область вызова.

Если инструменту нужны сразу несколько таких параметров, они просто
перечисляются в подписи. Так устроен `diagram_save` плагина
`boba-tool-canvas`: аргументы модели `name` и `spec`, а дальше `subject`,
`scope`, `root` и `cfg`:

```python
@tool
async def diagram_save(
    name: Annotated[str, Field(min_length=1, description=DiagramPrompt.NAME)],
    spec: Annotated[str, Field(min_length=1, description=DiagramPrompt.SPEC), MarkdownResult(language="mermaid")],
    subject: Annotated[Subject, Injected],
    scope: Annotated[Scope, Injected],
    root: Annotated[WorkspaceRoot, Injected],
    cfg: Annotated[CanvasToolConfig, Injected],
) -> CanvasResult | ErrorResult:
```

Третья метадата у `spec` — `MarkdownResult(language="mermaid")` — говорит
чату, как показывать значение аргумента в ленте (блок кода с подсветкой): в
схеме инструмента она становится стандартным ключом `contentMediaType`
(`text/vnd.mermaid`). На тело она не влияет.

Connection и порты появятся в разделах 5 и 7. Сейчас нужны первые два.

Контекст вызова нужен телу, которое работает от имени пользователя
(`Subject`) или с файлами треда (`Scope` и `WorkspaceRoot`). Так выглядит
`send_file` плагина `boba-tool-canvas`:

```python
@tool
async def send_file(
    path: Annotated[str, Field(min_length=1, description=CanvasPrompt.FILE_PATH)],
    subject: Annotated[Subject, Injected],
    scope: Annotated[Scope, Injected],
    root: Annotated[WorkspaceRoot, Injected],
) -> FileResult | ErrorResult:
    """Отправить пользователю файл из workspace вложением в чат."""
    try:
        key = ThreadFiles(subject, scope, root).existing(path)
    except CanvasError as e:
        return e.failure()

    return FileResult(path=key.in_workspace(), name=key.name, mime=ContentMime.of_name(key.name))
```

`ThreadFiles` зовёт `root.apply()` и строит ключ через `CanvasScope.key` из
`boba-canvas`, а тот принимает только пути своего треда: `/workspace/<thread_id>/upload/...` в
песочнице, `<workdir>/<thread_id>/upload/...` в режиме `process`. Всё, что
тело делает с чатом, умещается в возврате `FileResult`: хост отдаёт его
ссылкой на файл, а чат показывает её вложением к шагу.

### Модель конфига и тело

```python
"""Инструменты redis: функции уровня модуля, модуль — обычная программа.

Запуск: `python -m boba.tool.redis.tools redis_scan --pattern "user:*"`.

Ошибки:
RedisError — сервер недоступен или отверг команду.
"""

from __future__ import annotations

import sys
from collections.abc import Mapping
from typing import Annotated, ClassVar, Final

from pydantic import BaseModel, ConfigDict, Field
from redis.asyncio import Redis
from redis.exceptions import RedisError

from boba.toolkit.entry import ToolMain
from boba.toolkit.facade import Injected, tool
from boba.toolkit.result import TableResult
from boba.toolkit.sql import SqlErrorKind, SqlLimits


class RedisServer(BaseModel):
    """Адрес сервера из конфига администратора."""

    host: str = Field(min_length=1)
    port: int = Field(default=6379, ge=1)
    db: int = Field(default=0, ge=0)


class RedisToolConfig(SqlLimits):
    """Сервер и лимиты выдачи; секция [tool.redis]."""

    model_config = ConfigDict(extra="ignore")

    SECTION: ClassVar[str] = "tool.redis"

    server: RedisServer
    scan_batch: int = Field(default=500, ge=1)


class RedisKeyRow(BaseModel):
    """Строка выдачи redis_scan."""

    key: str
    type: str
    ttl: int


@tool
async def redis_scan(
    pattern: Annotated[
        str,
        Field(
            min_length=1,
            description=(
                "Шаблон ключей в синтаксисе SCAN MATCH, например 'user:*' "
                "или 'session:2026-*'. Выдача ограничена max_rows конфига."
            ),
        ),
    ],
    cfg: Annotated[RedisToolConfig, Injected],
) -> TableResult:
    """Перебрать ключи по шаблону с типом и TTL каждого."""
    client = Redis(host=cfg.server.host, port=cfg.server.port, db=cfg.server.db)

    rows: list[dict[str, object]] = []
    try:
        async for key in client.scan_iter(match=pattern, count=cfg.scan_batch):
            if len(rows) >= cfg.max_rows:
                break

            row = RedisKeyRow(
                key=key.decode(),
                type=(await client.type(key)).decode(),
                ttl=await client.ttl(key),
            )
            rows.append(row.model_dump())
    finally:
        await client.aclose()

    return TableResult(rows=rows)


EXPECTED: Mapping[type[Exception], SqlErrorKind] = {
    RedisError: SqlErrorKind.DATABASE_UNAVAILABLE,
}

TOOLS: Final = ToolMain.toolset(redis_scan)

if __name__ == "__main__":
    sys.exit(ToolMain.run(TOOLS))
```

Разберём, что здесь за что отвечает.

**`SECTION`** на модели конфига говорит хосту, из какой таблицы toml
собирать значение для параметра `cfg`. Хост читает `SECTION` с аннотации,
валидирует таблицу `[tool.redis]` этой моделью и кладёт результат в
JSON-канал под ключом `"cfg"`, то есть под именем параметра. Без `SECTION`
старт падает с текстом «… must define SECTION as the toml section name,
got None».

**`extra = "ignore"`** нужен потому, что в той же таблице toml лежат
служебные ключи `enable`, `tools`, `headless`, `sandbox`. Их читает
загрузчик хоста, а не ваша модель.

**`SqlLimits`** из `boba.toolkit.sql` даёт поля `max_rows` и `max_bytes`,
общие для всех инструментов с табличной выдачей. Инструменту без таблиц
подойдёт обычный `BaseModel` с `SECTION`.

**Докстринг** функции обязателен: это описание инструмента, которое читает
модель. **`Field(description=...)`** у аргумента модели — единственное, что
модель узнает про параметр, поэтому в описании нужен формат и пример
значения, как выше у `pattern`.

**Возврат** — модель результата, наследник `ToolResultBase` из
`boba.toolkit.result`: `MarkdownResult` для текста и кода, `TableResult` для
таблиц не из SQL, `SqlResult` для выдачи SQL любой базы, `ShellResult` для
команд, `VisualResult` для графиков и jsx-виджетов, `FileResult` для файла
workspace, который уходит вложением в чат, `CanvasResult` для файла,
который показывается в панели канваса. Аннотация возврата обязана назвать
класс. Результат хост отдаёт стандартными блоками ответа MCP, и чат рисует
их по типу содержимого, а не по имени инструмента. За это отвечают два
метода семейства:

- `llm_view()` — текст для модели; он уходит блоком `text` с
  `annotations.audience = ["assistant"]`;
- `shown(out)` — блоки для человека в приёмник `ContentOut`; `sent(out)`
  пишет оба набора подряд, а `McpContent` (`boba.toolkit.wire`) превращает
  их в блоки MCP.

| Результат | Блок для человека |
|---|---|
| `MarkdownResult` | `text`; с языком — встроенный ресурс `resource` с MIME языка (`application/sql`, `text/vnd.mermaid`, ...) |
| `TableResult` | `resource` `text/csv` |
| `SqlResult` | `resource` `application/sql` с текстом запроса и `resource` `text/csv` с выдачей, если она есть |
| `ShellResult` | `resource` `text/plain` с показанным потоком (`stdout`, а если показан не он — `stderr`) и строка итога |
| `VisualResult` | подпись; у графика plotly — `resource` `application/vnd.plotly.v1+json` с фигурой |
| `FileResult` | `resource_link` на файл workspace |
| `CanvasResult` | `resource_link` с `annotations.priority = 1.0`: чат открывает файл в панели треда |
| отказ (`ErrorResult` и родня) | `text`, а ответ помечен `isError` |

Тело ничего не знает про чат: файл оно пишет в смонтированный workspace и
возвращает результат. Ссылку с приоритетом открывает панель треда в чате.
Если вьювер браузера ответил, что файл не отрисовался (mermaid), отказ
показа превращает в `isError` того же вызова ретранслятор чата — так отказ
доходит до модели тем же путём, что любой отказ инструмента. Образец —
плагин `boba-tool-canvas` (`canvas_open`, `send_file`, `diagram_save`,
секция `[tool.canvas]` с `workspace = true`). Телу, которому нужен корень
workspace или тред вызова, хост подаёт их injected-параметрами
`root: Annotated[WorkspaceRoot, Injected]` и
`scope: Annotated[Scope, Injected]`.

**`EXPECTED`** — карта «исключение → вид отказа». Исключение из этой карты
уезжает конвертом `ReplyError` и показывается пользователю текстом. Любое
другое исключение считается дефектом: трейсбек в stderr, код выхода не
ноль, хост поднимает `LauncherError` с хвостом stderr.

**`TOOLS` и блок `__main__`** делают модуль программой. Одну и ту же
команду `python -m boba.tool.redis.tools redis_scan --pattern "user:*"`
исполняет launcher и человек в терминале.

### Манифест и pyproject

Манифест — единственное, что хост читает у плагина, чтобы узнать его
инструменты:

```python
"""Манифест плагина redis: entry point группы boba.tools."""

from typing import Final

from boba.tool.redis.tools import TOOLS
from boba.toolkit.manifest import ToolPluginManifest

MANIFEST: Final = ToolPluginManifest(section="redis", tools=tuple(TOOLS))
```

Пакет объявляется целиком, без общих «утилитных» зависимостей на всякий
случай:

```toml
[build-system]
requires = ["setuptools>=64", "wheel"]
build-backend = "setuptools.build_meta"

[project]
name = "boba-tool-redis"
version = "0.0.19.dev4"
description = "Инструменты redis: перебор ключей на сервере из конфига."
requires-python = ">=3.11,<4"
dependencies = [
    "boba-toolkit==0.0.19.dev4",
    "pydantic>=2.10,<2.11",
]

[project.optional-dependencies]
# зависимости тела: ставятся только в образ песочницы,
# в окружении приложения их быть не должно
payload = [
    "redis>=5",
]
dev = [
    "boba-config==0.0.19.dev4",
    "boba-stand-core==0.0.19.dev4",
    "pytest>=8",
]

[project.entry-points."boba.tools"]
redis = "boba.tool.redis.plugin:MANIFEST"

[tool.setuptools.packages.find]
where = ["src"]
include = ["boba*"]
namespaces = true

[tool.boba.sandbox]
imports = ["redis"]
```

Что где лежит:

- **`dependencies`** — то, без чего не читаются объявления: `boba-toolkit`,
  `pydantic`, пакеты моделей конфига и профилей. Их ставит и хост, и
  песочница. Версии boba-пакетов пинятся точно, их проставляет
  `make version-set`.
- **`payload`** — клиент, парсер, движок: всё, чем работает тело. Хост эту
  группу не ставит и не импортирует, поэтому плагин остаётся
  самостоятельной программой со своими зависимостями. Как не притащить их
  на хост случайным импортом, сказано в разделе 9.
- **`dev`** — только для тестов пакета.
- **`entry-points`** — имя группы `boba.tools`, ключ равен имени секции
  конфига, значение указывает на `MANIFEST`.
- **`[tool.boba.sandbox]`** — декларации сборщика образа; ключ `imports`
  перечисляет модули смоук-проверки после сборки: если `redis` в образ не
  попал, сборка упадёт здесь, а не на первом вызове в проде. Остальные
  ключи секции в разделе 8.

Entry point материализуется установкой: после правки pyproject нужен
`uv sync --all-packages --all-extras`.

### Файл конфига

Хост не стартует, пока для установленного плагина нет файла
`conf/plugins/redis.toml`. Файл плагина — часть общего конфига boba-mcp и
лежит в git рядом с ним: `packages/apps/boba-mcp/conf/plugins/redis.toml`.
Сборка кладёт его в образ сервиса, а отладка читает прямо из пакета;
развёртыванию остаётся только site.toml со значениями, на которые файл
ссылается.

```toml
enable     = true
tools      = ["redis_scan"]
max_rows   = 200
max_bytes  = 1000000
scan_batch = 500

[server]
    host = "redis.corp"
    port = 6379
    db   = 0

[sandbox]
    network = true
    binds   = ["/etc/resolv.conf:/etc/resolv.conf", "/etc/hosts:/etc/hosts"]
```

- `enable = false` выключает секцию целиком: плагин не загружается.
- `tools` — список инструментов, которые получит модель. Инструмент есть
  в `TOOLS`, но не в списке — модели он не виден. Так администратор
  управляет набором.
- `headless` — подмножество `tools` для вызовов вне хода чата. Ключ
  остался в модели конфига (`PluginMeta.headless`), но ни один плагин
  поставки его не задаёт. boba-mcp публикует по MCP и такие инструменты:
  список его endpoint'а собирается так же, как для вызовов вне чата.
- Остальные ключи — поля `RedisToolConfig`, вложенная таблица `[server]`
  — поля `RedisServer`.
- `[sandbox]` — изоляция для sandbox-режима. По умолчанию у тела нет сети,
  поэтому для инструмента, который ходит на сервер, нужен `network = true`
  и бинды `resolv.conf` и `hosts`, иначе имя `redis.corp` не разрешится.
  Все ключи секции в разделе 8.

### Что получает тело

Вызов `redis_scan(pattern="user:*")` хост превращает в две части:

```
argv:      python3 -m boba.tool.redis.tools redis_scan --pattern "user:*"
injected:  {"cfg": {"max_rows": 200, "max_bytes": 1000000, "scan_batch": 500,
                    "server": {"host": "redis.corp", "port": 6379, "db": 0}}}
```

Имя параметра становится флагом с заменой `_` на `-`: параметр `scan_limit`
дал бы флаг `--scan-limit`. Строки едут как есть, остальные типы JSON.
Ключ JSON-канала равен имени параметра: второй injected-параметр
`limits: Annotated[LimitsConfig, Injected]` добавил бы ключ `"limits"` со
своей секцией, а параметры контекста — ключи с их моделями. Для
`send_file(path=...)` из предыдущего раздела JSON выглядит так:

```
injected:  {"subject": {"user_id": "f8920970-…", "login": "ivanov", "roles": ["DEV"], "profile": "general"},
            "scope":   {"kind": "chat", "id": "38586395-…"},
            "root":    {"path": "/workspace"}}
```

Тело валидирует флаг `--pattern` типом поля и ключ `"cfg"` моделью
`RedisToolConfig`, потом зовёт `await redis_scan(pattern=..., cfg=...)`.
Нет ключа или значение не проходит модель — конверт `ReplyError` с видом
`invalid_request`.

Так же устроен плагин `doc` в `packages/tools/boba-tool-doc`: модель
`DocToolSection` с `SECTION = "tool.doc"` и файл `conf/plugins/doc.toml`.

---

## 3. Секрет в конфиге

У сервера появился пароль. Он обязан доехать до тела, но не попасть ни в
argv, ни в лог, ни в трейсбек. Для этого в `RedisServer` добавляется поле
типа `SecretStr`, а модель конфига наследует `SecretRevealing` вместо
`BaseModel`:

```python
from pydantic import SecretStr

from boba.toolkit.types import SecretRevealing


class RedisServer(BaseModel):
    """Адрес и пароль сервера из конфига администратора."""

    host: str = Field(min_length=1)
    port: int = Field(default=6379, ge=1)
    db: int = Field(default=0, ge=0)
    password: SecretStr = Field(min_length=1)


class RedisToolConfig(SecretRevealing, SqlLimits):
    """Сервер и лимиты выдачи; секция [tool.redis]."""

    model_config = ConfigDict(extra="ignore")

    SECTION: ClassVar[str] = "tool.redis"

    server: RedisServer
    scan_batch: int = Field(default=500, ge=1)
```

Что делает каждая часть.

`SecretStr` сам по себе только маскирует: `repr`, `model_dump` и трейсбек
показывают `**********`. Но JSON-канал в тело — это тоже дамп, и без
дополнительных мер тело получило бы звёздочки вместо пароля и упало бы
непонятной ошибкой авторизации на первом запросе.

`SecretRevealing` даёт модели метод `revealed()`. Хост перед отправкой в
тело проверяет, есть ли у значения такой метод, и зовёт его вместо обычного
дампа. Метод обходит модель на любую глубину, включая вложенные модели,
списки и словари, и заменяет каждый `SecretStr` открытой строкой. Никаких
сериализаторов писать не нужно. На стороне тела ничего делать не надо:
JSON валидируется в ту же модель, и открытая строка снова становится
`SecretStr`.

Единственное исключение: поле со своим `@field_serializer` обход не трогает,
считается, что у поля своя политика. Так сделано у kerberos-секций: пароль
и keytab маскируются всегда, потому что в тело едет билет, а не они
(раздел 6).

В теле секрет читается через `get_secret_value()` в момент использования и
не сохраняется в переменные:

```python
client = Redis(
    host=cfg.server.host,
    port=cfg.server.port,
    db=cfg.server.db,
    password=cfg.server.password.get_secret_value(),
)
```

В сообщения об ошибках нельзя форматировать ни `cfg`, ни параметры
подключения целиком. У `RedisError` в тексте пароля нет, поэтому карта
`EXPECTED` остаётся прежней.

В файл плагина пароль напрямую не пишется: файл лежит в git и одинаков во
всех развёртываниях. Секрет живёт в site.toml развёртывания, в таблице
`[site]`, а файл плагина ссылается на него интерполяцией:

```toml
[server]
    host     = "redis.corp"
    port     = 6379
    db       = 0
    password = "${site.redis_password}"
```

Целую таблицу можно подключить одной ссылкой: `server = "${site.redis}"`
возьмёт таблицу `[site.redis]` из site.toml вместе с паролем. Ссылаться
можно и на секцию общего конфига приложения, которая сама собрана из
значений site.toml: так сделано у плагина `kb`, `connection = "${postgres}"`.
Какие ключи site.toml нужны приложению вместе с плагинами, печатает
`make -C build site-keys APP=mcp`.

Так устроен `KbToolConfig` плагина `kb`: модель наследует
`SecretRevealing`, а внутри лежит целый `PostgresConfig` с паролем.

---

## 4. Тип connection: свой connection у каждого пользователя

Сервер в конфиге администратора — это один сервер на всех. Обычно нужно
иначе: у пользователя есть свой redis, выданный ему лично или его роли, и
модель по имени выбирает, куда идти. Такие connection хранит чат, в своей
схеме базы (`aid` по умолчанию): таблица `connections` — id, имя, описание и
`data jsonb` с профилем, секретная часть которого зашифрована ключом
`[keys].encryption` общего конфига чата; таблицы `roles` и `grants`
описывают, кому какой connection выдан. Заводит строки страница
«Connections» приложения studio, но studio в поставку не входит и не
собирается. Своего способа завести строку у чата нет, поэтому строки кладут
хранилищем `ConnectionStore` пакета `boba-connection-broker`, которое само
шифрует секреты, — так делают и тесты (раздел 10). Сервис boba-mcp этих
таблиц не читает: профиль приходит к нему от чата запечатанным (раздел 5).

Чтобы хост умел разбирать строку таблицы в модель, у каждого профиля есть
поле `kind`. По нему реестр типов connection, собираемый из entry points
группы `boba.connections`, находит модель профиля и функцию пробы. Проба —
проверка соединения по кнопке «Check» на странице «Connections» studio;
чат и boba-mcp её не зовут.

Тип connection и плагин инструментов — разные вещи с разными entry points.
Их можно положить в один пакет или в два. Postgres, clickhouse и web
сделаны двумя: тип живёт в инфра-пакете (`packages/infra/db/boba-db-postgres`),
инструменты в `packages/tools/boba-tool-postgres` и зависят от него. Так
нужно, когда типом пользуется несколько плагинов или когда тип нужен чату
отдельно от инструментов: чат разбирает строки таблицы, выпускает к ним
билеты и запечатывает профили, а плагинов не загружает. Для redis типом
пользуется только наш плагин, поэтому объявим оба entry point в одном
пакете:

```
packages/tools/boba-tool-redis/
    pyproject.toml
    src/boba/tool/redis/
        connection.py    # модель соединения
        manifest.py      # MANIFEST типа    -> boba.connections
        tools.py         # инструменты
        plugin.py        # MANIFEST плагина -> boba.tools
```

У одного пакета есть следствие. Модуль с манифестом типа импортируется
хостом при старте, и неудачный импорт роняет запуск. Поэтому всё, что
`connection.py` тянет на уровне модуля, обязано лежать в обычных
`dependencies`, а не в `payload`. Клиент redis попадает туда неизбежно:
проба ходит на сервер по-настоящему.

```toml
[project]
name = "boba-tool-redis"
version = "0.0.17.dev3"
dependencies = [
    "boba-toolkit==0.0.17.dev3",
    "boba-connections==0.0.17.dev3",
    "redis>=5",
]

[project.entry-points."boba.connections"]
redis = "boba.tool.redis.connection:MANIFEST"

[project.entry-points."boba.tools"]
redis = "boba.tool.redis.plugin:MANIFEST"
```

Имя entry point в группе `boba.connections` обязано совпадать с `kind`
профиля, реестр проверяет это на старте.

### Профиль

Соединение наследует `ConnectionBase` из `boba.connections.base`.
Базовый класс даёт поля `kind`, `description` и `source`, а от наследника
требует метод `trace()`; остальные методы переопределяются по
необходимости.

```python
"""Профиль connection redis."""

from __future__ import annotations

from typing import ClassVar, Literal

from pydantic import Field, SecretStr

from boba.connections.base import ClientIdentity, ConnectionBase


class ClientName:
    """Подпись сессии для redis: CLIENT SETNAME режет длинное и не терпит пробелов."""

    MAX_BYTES: ClassVar[int] = 63
    SEPARATOR: ClassVar[str] = ":"

    @classmethod
    def of(cls, client: ClientIdentity) -> str:
        joined = cls.SEPARATOR.join((client.application, client.login, client.tool))

        raw = joined.encode("utf-8")
        if len(raw) <= cls.MAX_BYTES:
            return joined

        return raw[: cls.MAX_BYTES].decode("utf-8", errors="ignore")


class RedisConnection(ConnectionBase):
    """Подключение к redis: адрес, база и пароль."""

    kind: Literal["redis"] = Field(
        default="redis",
        description="Дискриминатор connection при хранении в базе.",
    )

    host: str = Field(min_length=1)
    port: int = Field(default=6379, ge=1)
    db: int = Field(default=0, ge=0)
    password: SecretStr = Field(min_length=1)
    client_name: str = Field(default="")

    def trace(self) -> str:
        return f"auth=password host={self.host} db={self.db}"

    def labeled(self, client: ClientIdentity) -> RedisConnection:
        return self.model_copy(update={"client_name": ClientName.of(client)})
```

- `kind: Literal["redis"]` хранится в jsonb каждой строки. Менять потом
  нельзя: строки в базе перестанут находить модель.
- `description` наследуется от базы. Это текст, который модель читает в
  `connection_list`, выбирая connection под задачу (раздел 5). Заполняет
  его тот, кто заводит строку.
- `trace()` — строка журнала «под кем идём». Хост пишет её по профилю,
  который реально уедет в тело.
- `labeled(client)` — подпись сессии, если сервер такое умеет. Хост знает
  только `ClientIdentity`: приложение, логин, инструмент. Как их собрать и
  куда положить, решает профиль: у postgres это `application_name` с
  пределом в 63 байта, у clickhouse — `client_name`. Тело потом
  передаёт `client_name` в `CLIENT SETNAME`, и в `CLIENT LIST` на сервере
  видно `boba:ivanov:redis_query`. Сервер без такого поля метод не
  переопределяет: база возвращает профиль как есть.
- `password: SecretStr`, и больше ничего: в таблице поле шифруется само, а
  в тело раскрывается тем же обходом, что в разделе 3. Профили не
  наследуют `SecretRevealing`: раскрытие для них делает хост, когда
  кладёт профиль в JSON-канал.

### Манифест с пробой

```python
"""Тип connection redis: манифест для реестра boba.connections.

Ошибки:
ConnectionTypeError — проба получила профиль чужого типа.
RedisError — сервер недоступен или отверг PING.
"""

from redis.asyncio import Redis

from boba.connections.base import ConnectionBase, ConnectionTypeError
from boba.connections.manifest import ConnectionTypeManifest
from boba.tool.redis.connection import RedisConnection

__all__ = ["MANIFEST"]


async def _probe(connection: ConnectionBase) -> str:
    if not isinstance(connection, RedisConnection):
        msg = f"redis probe expects a RedisConnection, got kind {connection.kind!r}"
        raise ConnectionTypeError(msg)

    client = Redis(
        host=connection.host,
        port=connection.port,
        db=connection.db,
        password=connection.password.get_secret_value(),
    )
    try:
        pong = await client.ping()
    finally:
        await client.aclose()

    return f"PONG {pong}"


MANIFEST = ConnectionTypeManifest(kind="redis", model=RedisConnection, probe=_probe)
```

Исключения пробы глотать не надо: граница превратит их в результат
«проверка не прошла» с текстом.

После `uv sync --all-packages --all-extras` тип виден реестру:

```bash
.venv/bin/python -c "from boba.connections.manifest import ConnectionTypes; print(ConnectionTypes.discover().kinds())"
```

Страница «Connections» studio строит форму нового типа сама, из json-schema
модели профиля. Если пакет типа удалить, строки его вида списки соединений
пропускают с предупреждением в лог.

Образец в репозитории: `PostgresConfig` в пакете `boba-db-postgres`, где
проба выполняет `select version()` и возвращает версию сервера.

---

## 5. Инструмент с connection пользователя

Теперь перепишем инструменты так, чтобы сервер приходил из connection
пользователя, а не из конфига. Connection объявляется прямо в подписи: тип
параметра — модель профиля, маркер `UserConnection` рядом:

```python
from boba.toolkit.facade import Injected, UserConnection, tool

RedisTarget = Annotated[RedisConnection, UserConnection]
```

У такого параметра две стороны. Для модели это строка: имя connection,
которое она выбирает по выдаче `connection_list`. Хост правит схему при
загрузке: вместо модели профиля в схеме строка с пометкой
`x-boba-connection` и видом connection, а что на её месте можно передать,
говорят стандартные ключи схемы (подробно — ниже, «Что предлагается
вызывающему»). Для тела это готовый профиль с паролем внутри. Ничего
регистрировать не нужно: вид connection хост выводит из типа параметра
через реестр типов.

Параметров-connection может быть несколько. Инструмент перекачки берёт
источник и приёмник:

```python
@tool
async def redis_copy(
    source: RedisTarget,
    target: RedisTarget,
    pattern: Annotated[
        str,
        Field(min_length=1, description="Шаблон ключей источника, например 'cache:*'."),
    ],
    cfg: Annotated[RedisToolConfig, Injected],
) -> MarkdownResult:
    """Скопировать ключи по шаблону с одного сервера на другой с сохранением TTL."""
    src = _client(source)
    dst = _client(target)

    copied = 0
    try:
        async for key in src.scan_iter(match=pattern, count=cfg.scan_batch):
            if copied >= cfg.max_rows:
                break

            dump = await src.dump(key)
            if dump is None:
                continue

            ttl_ms = await src.pttl(key)
            if ttl_ms < 0:
                ttl_ms = 0

            await dst.restore(key, ttl_ms, dump, replace=True)
            copied += 1
    finally:
        await src.aclose()
        await dst.aclose()

    return MarkdownResult(text=f"copied {copied} keys from {source.host} to {target.host}")


def _client(profile: RedisConnection) -> Redis:
    return Redis(
        host=profile.host,
        port=profile.port,
        db=profile.db,
        password=profile.password.get_secret_value(),
        client_name=profile.client_name,
    )
```

Обратите внимание, чего в теле нет: поиска connection по имени, проверки
прав, whitelist'а. Всё это сделали чат и хост до запуска. Конфиг секции при этом
никуда не делся: в нём живут лимиты и настройки администратора, а
`server` из него теперь можно убрать.

Отдельного ключа, который включал бы такие инструменты, нет. Но инструмент
с параметром-connection виден вызывающему, только если правила endpoint'а
boba-mcp что-то ему предлагают на месте этого параметра (ниже, «Что
предлагается вызывающему»); иначе инструмента нет в `tools/list`. Тело
может быть и `async def`, и обычной функцией: профиль и билет хост готовит
до тела.

Манифест и файл плагина остаются обычными. Профилей соединений
пользователей в файле нет: они приходят от чата на каждый вызов. Файл
может назвать заданные соединения сервера, которые годятся инструментам
плагина, — таблица `[connections]` со списком имён на инструмент (ниже,
«Заданные соединения сервера»).

```toml
enable     = true
tools      = ["redis_scan", "redis_copy"]
max_rows   = 200
max_bytes  = 1000000
scan_batch = 500

[sandbox]
    network = true
    binds   = ["/etc/resolv.conf:/etc/resolv.conf", "/etc/hosts:/etc/hosts"]
```

### Как модель узнаёт имена

Инструменты `connection_list` и `connection_search` — собственные
инструменты чата (`ConnectionTools` в `boba.connection_broker.tools`):
соединения и гранты хранит чат, поэтому они исполняются в его процессе и
читают таблицы connections/roles/grants хранилищем брокера. Агенту чат
объявляет их отдельным сервером с именем `chat`, рядом с серверами boba-mcp;
какие из них получит модель, задаёт список `tools` профиля чата
(`[profiles.<имя>].tools` общего конфига чата). Кто спрашивает, инструмент
узнаёт из контекста вызова.

В выдаче соединение названо именем строки; модель ставит его на место
параметра-соединения, а ретранслятор чата перед отправкой вызова сервису
boba-mcp заменяет имя запечатанным профилем (`CallSealing` в
`boba.connection_broker.sealing`). Какой параметр — соединение и какого вида,
чат узнаёт из схемы инструмента, которую отдал сервис: по пометке
`x-boba-connection` и ветке со стандартными ключами `contentMediaType:
application/jose` и `contentSchema` — схемой содержимого, где поле `kind` —
константа вида (`SealedParamSchema` в `boba.connections.sealed`). Модель
видит ту же схему и её описание параметра: «Name of the postgres connection
to use. Pass the name of one of your own connections, the client seals it.»

Выдача `connection_list` — все connection, доступные пользователю лично или
любой его роли:

| name | kind | url | host | description |
|---|---|---|---|---|
| `cache` | `redis` | `redis://redis01.corp:6379/0` | `redis01.corp` | кэш сессий прода, только чтение |
| `cache-staging` | `redis` | `redis://redis-stg.corp:6379/0` | `redis-stg.corp` | кэш стейджинга, можно писать |
| `analytics` | `postgres` | `postgresql://dwh01.corp:5432/dwh` | `dwh01.corp` | витрины продаж |

Колонку `url` строит пакет типа соединения: манифест типа
(`ConnectionTypeManifest`) рядом с пробой объявляет хук `address`, который
по профилю отдаёт адрес сервера и базы без учётных данных той же грамматикой,
что и адреса объектов (`PgAddresses.url_of`, `ChAddresses.url_of`, ...).
Профиль, у которого адреса не собрать (нет хоста), получает в колонке текст
причины. Инструменты `pg_address`, `ch_address`, `ora_address`, `web_address`
отвечают тем же url для одного соединения.

`connection_search` отдаёт ту же раскладку, но отбирает строки фильтрами
по колонкам, которые складываются по И: `kind` — точное совпадение вида,
`name` и `host` — подстрока без учёта регистра, `description` — слова,
каждое из которых должно встретиться в описании. Пустой фильтр не
применяется, вызов без фильтров равен `connection_list`.

По `kind` модель понимает, какому инструменту имя годится, по описанию
выбирает под задачу. Описание берётся из поля `description` профиля, хост
— из ключа `host` jsonb, поэтому типы соединений называют поле адреса
именно так. Секреты профилей тело не читает — только открытые ключи jsonb.

Граф грантов один на всех: доменный `SubjectGrantsQuery`
(`boba.access.grants`) отдаёт текст SQL и параметры, а запечатывание в чате
и инструменты соединений лишь подставляют идентификаторы своих схем
(`SqlNames.mapping` по картам `ConnectionNames`) и исполняют его своим
подключением. Запрос считает `copies` — сколько строк субъекта носят имя
внутри вида — по всем выданным строкам, а фильтры `ConnectionFilter`
отбирают строки уже из них. Имя-дубль на вызове даёт
`AmbiguousConnectionError`, в выдачу инструментов не попадает
(`unique_only`), и фильтр поиска сделать его уникальным не может.

### Что происходит на вызове

Модель вызвала `redis_copy(source="cache", target="cache-staging",
pattern="cache:*")`. Работа делится между чатом и сервисом boba-mcp.

На загрузке хост нашёл у `redis_copy` два параметра с маркером и вывел их
вид из типа: `redis`. В схеме инструмента на их месте строки с пометкой
`x-boba-connection: "redis"`.

В чате, до отправки вызова сервису:

1. Ретранслятор читает из схемы инструмента, какие параметры — соединения и
   что сервер принимает на их месте.
2. Берёт субъект вызова из контекста вкладки: пользователь, логин, роли.
3. Одним SQL читает гранты на пользователя и его роли с фильтром по виду
   `redis` и группирует по имени. Имя, выданное дважды с разными строками
   (лично и ролью), попадает в список неоднозначных.
4. Выбирает строку по каждому имени. Нет такой — отказ с перечнем
   доступных имён, чтобы модель исправилась и повторила вызов; модель
   получает его результатом вызова с `isError`. Дубль — отказ
   `ambiguous_connection`. Имя заданного соединения сервера, которого нет
   среди соединений пользователя, уходит как есть.
5. Для kerberos-типов меняет kerberos-секцию на билет вызова (раздел 6).
6. Запечатывает профиль ключом сервера из его возможностей (расширение
   `com.boba/connection-seal`) со сроком `[agent].seal_ttl_sec` и ставит
   запечатанное значение на место имени.

В сервисе boba-mcp, на вызове:

1. Проверяет значение по тому же предложению, по которому строил схему:
   имя предложенного соединения сервера, затем запечатанное значение, если
   оно предложено; иначе отказ с предложенными именами.
2. Имя соединения сервера берёт профилем этого соединения; запечатанное
   значение открывает своим ключом, проверяет срок и вид.
3. Зовёт `profile.labeled(client)`: профиль подписывает сессию сам.
4. Для kerberos-секции заданного соединения сервера выпускает билет вызова
   по его keytab или паролю.
5. Имена остаются строками в argv, профили уезжают JSON-каналом под
   ключами `"source"` и `"target"`.
6. Тело собирает JSON обратно в два `RedisConnection` и работает.

В тело уезжают ровно те профили, которые назвал вызов. Остальные connection
пользователя туда не попадают, даже именами.

### Что предлагается вызывающему

Что предложить вызывающему на месте параметра-соединения, решают правила
endpoint'а в общем конфиге boba-mcp, `[[mcp.endpoints.<имя>.connections]]`.
Правила проверяются по порядку, срабатывает первое подходящее:

- `roles` — для кого: хотя бы одна роль вызывающего, `*` — все;
- `client` — `sealing` (клиент объявил расширение `com.boba/connection-seal`,
  как наш чат), `plain` (не объявил) или `any`;
- `offer` — что предложить: `sealed` (соединения пользователя,
  запечатанные клиентом) и/или `server` (заданные соединения сервера).

Если не подошло ни одно правило или предложить нечего, инструмента с таким
параметром в `tools/list` нет. От предложения зависит схема параметра:

| Предложено | Схема параметра |
|---|---|
| `sealed` | строка с `contentMediaType: "application/jose"` и `contentSchema` с видом |
| `server` | строка с `enum` видимых вызывающему имён, их описания — в `description` |
| оба | `anyOf` из двух веток выше |

Пометка `x-boba-connection` с видом стоит в каждом случае. В поставке у
каждого endpoint'а одно правило: клиенту, который умеет запечатывать,
предлагаются соединения пользователя.

### Заданные соединения сервера

Соединение под сервисной учёткой описывается один раз в общем конфиге
boba-mcp, со своими ролями, а у инструмента — только список имён:

```toml
[connections.cache]
    kind        = "redis"
    description = "общий кэш сессий, только чтение"
    roles       = ["*"]
    connection  = "${site.connections.cache}"
```

`kind` — вид connection, `description` — текст для модели, `roles` — кому
соединение видно, `connection` — профиль ссылкой на таблицу site.toml (там
же пароль или keytab; делегирования у таких соединений нет). В файле
плагина `redis.toml` инструменты называют имена, которые им годятся:

```toml
[connections]
    redis_copy = ["cache"]
```

Вызывающий видит имена из списка инструмента, у которых совпала хоть одна
его роль, если правила endpoint'а предлагают ему `server`. Сервис не стартует, если
профиль не разбирается своим видом («[connections.cache]: the profile does
not parse as a connection of kind 'redis'»), если список называет
незаданное соединение («… which is not among [connections]») или
соединение чужого вида. На вызове значение вне предложения отвергается:
«unknown connection 'x': expected one of …», а запечатанное значение там,
где предложены только соединения сервера, — «sealed connections are not
offered to this caller, use one of: …».

### Проверка хоста для web

У типа `web` профиль `HttpConnection` покрывает свой хост, точным
именем или шаблоном `*.corp.example`. Адрес хранится частями с именами
аргументов `httpx.URL` (`scheme`, `username`, `password`, `host`, `port`,
`path`, `query`, `fragment`, `userinfo`, `netloc`, `raw_path`): заданная
часть подставляется в URL, незаданная — нет. URL собирают только
`root_url()` и `url_of(path)` средствами `httpx.URL`, вручную адреса не
склеиваются. Хост не знает, какой URL
инструмент собирается открыть, поэтому проверка на стороне тела:

```python
@tool
async def web_fetch_page(
    url: Annotated[str, Field(min_length=1, description="URL для скачивания")],
    connection: Annotated[HttpConnection, UserConnection],
    *,
    cfg: Annotated[WebToolsConfig, Injected],
) -> MarkdownResult:
    """Скачивает URL соединением connection (см. connection_list)."""
    bound = connection.for_url(url)
```

`HttpConnection.for_url` проверяет покрытие и возвращает соединение,
привязанное к конкретному хосту. Чужой хост — `UnknownHostError`,
объявленный в `EXPECTED` модуля как `unknown_host`. Тело ответа web-тул
не разбирает сам: поток уходит в `DocumentRouter` из `boba-doc`, тот же,
что читает вложения Confluence и файлы workspace, поэтому по ссылке
приходит текст любого поддерживаемого документа (html как markdown, pdf,
docx, xlsx, pptx, картинки через OCR).

Образец в репозитории: плагин `pg` объявляет
`PgConnection = Annotated[PostgresConfig, UserConnection]` и строит на нём
свои инструменты. Обе стороны connection живут в пакете
`boba-connection-broker`: поиск строки по грантам и запечатывание
(`ArmedConnections`, `CallSealing`) работают в процессе чата, открытие
запечатанного значения и заданные соединения сервера
(`SealedConnectionParams`, `SealedConnectionParam`) — в boba-mcp.

---

## 6. Kerberos: что тип обязан уметь

Redis kerberos не умеет, поэтому этот раздел про postgres и web. Строка
таблицы может нести kerberos-секцию трёх видов:

- `{method = "kerberos_delegated"}`: в сервис идёт сам пользователь.
  Работает, если он вошёл через SSO и браузер делегировал креды. Иначе
  отказ `no_delegated_credentials`.
- `{method = "kerberos_keytab", principal, keytab}`: сервисная учётка,
  keytab лежит на машине приложения, которое выпускает билет.
- `{method = "kerberos_password", principal, password}`: то же, но паролем.

Ни keytab, ни пароль в тело не уезжают. Билет выпускает тот, у кого креды:
для соединения пользователя — чат, перед запечатыванием (раздел 5), для
заданного соединения сервера и для профиля из файла плагина — boba-mcp на
вызове. Выпускается **один сервисный билет** к конкретному сервису, и
секция профиля подменяется на
`{method = "kerberos_ticket", ccache}` с этим билетом в base64. Форма
профиля не меняется, и тело не знает, как билет получен. TGT в ccache нет,
выпустить билет к другому сервису тело не может.

Чтобы чат и хост могли это сделать, не зная устройства профиля, профиль
реализует три метода базового класса. Так они выглядят у `PostgresConfig`:

```python
def kerberos_section(self) -> KerberosAuthBase | None:
    if isinstance(self.auth, KerberosAuthBase):
        return self.auth

    return None

def service_name(self) -> str:
    if not self.host:
        msg = "postgres connection: kerberos SPN needs host, hostaddr alone is not enough"
        raise ValueError(msg)

    if not isinstance(self.auth, KerberosAuthBase):
        msg = f"postgres connection to {self.host}: auth {self.auth.method} is not kerberos"
        raise ValueError(msg)

    return f"{PostgresKerberos.service_of(self.auth)}@{self.host}"

def with_call_ticket(self, ticket: TicketAuth) -> PostgresConfig:
    return self.model_copy(update={"auth": ticket})
```

- `kerberos_section()` говорит, где в профиле лежит секция; `None` — тип
  аутентифицируется иначе, и хост ничего не делает.
- `service_name()` — SPN в форме `service@host`, к которому выпускать
  билет: `postgres@db01.corp`, `HTTP@wiki.corp`. Для web с шаблоном хостов
  SPN получить нельзя, поэтому kerberos-connection web указывают точный
  `host`.
- `with_call_ticket(ticket)` — копия профиля с билетом на месте секции.

Защита от утечки встроена в сериализатор поля `auth`: если до дампа в
JSON-канал дошёл keytab или пароль, дамп падает с текстом «credentials may
not leave the application». Это ожидаемое поведение, а не баг.

В теле открытие подключения к базе оборачивается кредами. Так делает
`PayloadPostgres.connect_config`:

```python
credentials = ClientCredentials.of(connection.auth)

async with credentials.applied_async():
    conn = await PayloadPostgres._connect(connection)
```

`applied_async()` кладёт байты ccache во временный файл вызова (в песочнице
это приватный tmpfs), выставляет `KRB5CCNAME` и удаляет файл по выходу.
Для kerberos-типов в `[sandbox]` файла плагина нужен бинд
`"${env.krb}/krb5.conf:/etc/krb5.conf"`, как в `pg.toml` и `web.toml`.

То же самое работает для kerberos-профиля в статическом конфиге
администратора (раздел 3), как `connection = "${postgres}"` у `kb`: хост
находит секцию внутри injected-значения и подменяет её билетом на каждом
вызове. Только `kerberos_delegated` там невозможен: делегировать некому,
сессии пользователя у статического конфига нет.

---

## 7. Потоковый инструмент: порты

`redis_copy` копирует ключи между двумя redis. А если нужно выгрузить ключи
в postgres или файл? Для этого потоковые инструменты связываются в один
граф: узлы графа — инструменты, и данные текут между ними через ядро по
каналам, не проходя ни через модель, ни через хост. Связывает их
инструмент `workflow` сервиса boba-mcp: модель одним вызовом перечисляет
узлы — инструмент и его аргументы — и называет каналы полями на месте
портов: выход узла пишет в канал `out="orders"`, вход другого узла читает
его `feed="orders"` (вход-список — `feeds=["a", "b"]`). Узлы, связанные
каналами, — группа «все или никто»: сбой любого из них отменяет результат
всей группы. Отдельный вызов потокового инструмента без партнёра получает
отказ плана.

Инструмент становится узлом графа, когда объявляет **порт** в подписи.
Единица обмена — кадр: JSON-заголовок с полем `kind` плюс тело байтами.
Заголовки описываются pydantic-моделями со строковым `Literal` в `kind`, и
порт типизируется их объединением:

```python
from typing import Literal

from boba.toolkit.ports import Inbound, Outbound


class KeysChunk(BaseModel):
    kind: Literal["redis.keys"] = "redis.keys"
    seq: int
    count: int


class ScanDone(BaseModel):
    kind: Literal["redis.done"] = "redis.done"
    total: int


@tool
async def redis_scan_stream(
    connection: RedisTarget,
    pattern: Annotated[str, Field(min_length=1, description="Шаблон ключей, например 'user:*'.")],
    out: Annotated[Outbound[KeysChunk | ScanDone], Injected],
    cfg: Annotated[RedisToolConfig, Injected],
) -> MarkdownResult:
    """Узел конвейера: отдаёт пары ключ-значение порциями в выходной порт."""
    client = _client(connection)

    seq = 0
    total = 0
    try:
        async for key in client.scan_iter(match=pattern, count=cfg.scan_batch):
            value = await client.get(key)
            if value is None:
                continue

            seq += 1
            total += 1
            body = key + b"\t" + value + b"\n"
            out.emit(KeysChunk(seq=seq, count=1), body)
    finally:
        await client.aclose()

    out.emit(ScanDone(total=total))

    return MarkdownResult(text=f"streamed {total} keys")
```

Принимающий узел объявляет `Inbound` и читает кадры циклом:

```python
@tool
async def redis_restore_stream(
    connection: RedisTarget,
    feed: Annotated[Inbound[KeysChunk | ScanDone], Injected],
    cfg: Annotated[RedisToolConfig, Injected],
) -> MarkdownResult:
    """Узел конвейера: принимает пары ключ-значение и пишет их на сервер."""
    client = _client(connection)

    written = 0
    try:
        for item in feed:
            if isinstance(item.head, ScanDone):
                break

            for line in bytes(item.body).splitlines():
                key, value = line.split(b"\t", 1)
                await client.set(key, value)
                written += 1
    finally:
        await client.aclose()

    return MarkdownResult(text=f"restored {written} keys")
```

Правила портов:

- Портов в подписи может быть несколько, в том числе списками:
  `feeds: Annotated[Sequence[Inbound[Chunk]], Injected]` — все входы разом,
  у каждого свой канал; выходы `Outbound` симметричны. Несколько видов
  кадров в одном канале — объединение моделей в одном порте. Кадр с `kind`
  вне объявления роняет вызов на границе, до тела.
- `item.head` — модель заголовка, `item.body` — `memoryview` на буфер
  кадра; для склейки нужен `bytes(item.body)`.
- `RawInbound` и `RawOutbound` — голые байты без кадров, для перекачки
  вроде `COPY ... TO STDOUT` → `COPY ... FROM STDIN`. Сырое совместимо
  только с сырым, стыковку с кадровым портом конвейер отвергает до старта.
- Запись в порт блокируется при медленном потребителе: залить хост тело
  не может.
- `return` остаётся: конверт — итог, кадры — то, что по дороге.
- В схеме для модели порт заменён полем канала с тем же именем: у
  одиночного порта — строка с именем канала, у порта-списка — список имён.
  Само значение порта строит песочница на вызове.

Регистрировать узел не нужно: порт в подписи уже делает инструмент
потоковым, а стыковку каналов по объявленным `kind` и совместимость сырых и
кадровых портов исполнитель проверяет по описанию графа до запуска.

Соединения в аргументах узлов запечатывает чат, хотя по схеме самого
`workflow` их не видно: аргументы узла — свободный объект. Для этого сервер
объявляет расширение вложенных вызовов `com.boba/nested-calls` с формой
узла (раздел 12), и ретранслятор чата по нему разбирает узлы и запечатывает
соединения каждого по схеме его инструмента.

Образец портов кадров в репозитории: `ch_stream_out` и `ch_stream_in`
плагина `ch`, которые гонят ответ сервера между двумя базами байт в байт, и
`pg_stream_out`/`pg_stream_in` плагина `pg` с контрактом колонок в первом кадре.

---

## 8. Песочница: изоляция и образ

В sandbox-режиме тело исполняется внутри образа корня
`sandbox/plugins/<пакет>/rootfs.ext4`, смонтированного только для чтения.
Два места описывают, что там происходит: секция `[sandbox]` файла плагина
задаёт изоляцию на каждый вызов, секция `[tool.boba.sandbox]` в pyproject
задаёт, что положить в образ при сборке.

### `[sandbox]` в файле плагина

Модель секции запрещает неизвестные ключи: опечатка — ошибка старта.

| Ключ | Что делает |
|---|---|
| `network` | `true` — сеть хоста; по умолчанию сети нет |
| `workspace` | `true` — монтирует ext4-образ воркспейса пользователя в `/workspace` и делает его рабочим каталогом; по умолчанию воркспейса нет, рабочий каталог `/tmp` |
| `binds` | пары `host:guest`, только явные файлы и каталоги хоста, read-only; пути через `${env.*}` |
| `[sandbox.limits]` | `process_memory_bytes` (1 GiB), `process_cpu_sec`, `process_file_bytes`, `process_open_files` (1024), `group_memory_bytes` (1 GiB), `group_cpu_percent` (100 = одно ядро), `group_pids_max` (256), `timeout_sec` (86400) |
| `[sandbox.zygote]` | `max_start_attempts`, `restart_backoff_sec`, `start_timeout_sec` |
| `profile` | полный профиль ссылкой `"${sandbox.profiles.<имя>}"` вместо всех ключей выше |

Так выглядит секция плагина `kb`, которому нужны сеть, kerberos, веса
модели и много памяти:

```toml
[sandbox]
    network = true
    binds   = [
        "${env.models}/fastembed:/var/cache/fastembed",
        "/etc/resolv.conf:/etc/resolv.conf",
        "/etc/hosts:/etc/hosts",
        "${env.krb}/krb5.conf:/etc/krb5.conf"
    ]
    [sandbox.limits]
        process_memory_bytes = 17179869184
        group_memory_bytes   = 8589934592
        group_cpu_percent    = 400
```

Ориентиры: `pg.toml` — сеть и `krb5.conf`; `bash.toml` — `workspace = true`;
`doc.toml` — воркспейс и бинд `tessdata`.

Тело, убитое лимитом, отчитаться не успевает, и вызов без конверта
песочница объясняет по коду возврата: 152 — `process_cpu_sec`, 153 —
`process_file_bytes`, 137 и 134 — память, таймаут — `timeout_sec`.

### `[tool.boba.sandbox]` в pyproject

Секцию читает `make -C build plugin-rootfs PLUGIN=<пакет>`.
Python-часть образа декларировать не нужно: в него автоматически ставится
закрытие `payload`-зависимостей пакета. Секция описывает остальное:

- `imports` — модули смоук-проверки после сборки.
- `apt` — нативные debian-пакеты: утилиты, разделяемые библиотеки, шрифты.
- `data` — пути внутри образа, куда развёртывание подмонтирует данные:
  веса моделей, словари OCR. Сборка создаёт точку монтирования, а сами
  данные приезжают биндом из `[sandbox] binds` файла плагина, как
  `"${env.models}/tessdata:/usr/share/tessdata"` у `doc`.
- `root` — каталог-оверлей внутри пакета, копируется поверх корня как есть.
- `setup` — shell-скрипт внутри пакета, исполняется после `apt` и `root`.

Сборка читает декларации не только вашего пакета, но и всех boba-пакетов по
закрытию зависимостей. `boba-tool-doc` зависит от `boba-liteparse`, и образ
doc получает `apt`, `data`, `root` и `setup` из liteparse. Поэтому каждая
декларация живёт у владельца стека: libreoffice и tessdata объявляет
liteparse, а doc про них не знает.

```toml
[tool.boba.sandbox]
data  = ["/usr/share/tessdata"]
apt   = ["imagemagick", "libreoffice-writer", "libreoffice-calc", "ghostscript", "fonts-dejavu-core"]
root  = "sandbox-root"
setup = "sandbox-setup.sh"
```

После правок `boba-toolkit` или `boba-sandbox` образы всех плагинов
пересобираются (`make plugin-rootfs-all`): гость внутри rootfs отстаёт от
хоста по протоколу каналов. Песочница и образы корней плагинов нужны
одному хосту — boba-mcp; сборка кладёт их в `runtime/sandbox` (руководство
администратора, раздел 3), отладка читает их оттуда. Чату они не нужны. В
образ boba-mcp образы плагинов и шаблон workspace копирует последняя
стадия его сборки, поэтому снаружи контейнеру сервиса нужны только конфиг
и данные; пересобранный плагин попадает в сервис после `make build-mcp` и
пересоздания контейнера.

### Прогрев зиготы

Если тело на каждом вызове поднимает что-то тяжёлое (модель ONNX, словарь),
это можно сделать один раз в зиготе, и форки получат результат через
copy-on-write. Для этого объявляется корутина с декоратором `@warmup` и
единственным параметром-моделью конфига. Так делает `kb`:

```python
LLM: Final = LlmProviders(LlmProviderTypes.installed())


@warmup
async def warm_embedder(cfg: KbWarmupConfig) -> None:
    """Модель ONNX поднимается в зиготе: дети берут её через COW."""
    embedder = LLM.embedding(cfg.embedding)
    await embedder.embed_query("warm-up")
```

Конфиг прогрева хост собирает из той же секции `[tool.<секция>]` с
раскрытыми секретами. В режиме `process` прогрев не действует.

---

## 9. Зависимости и импорты: граница хоста и песочницы

Пакет инструмента установлен дважды: в окружении приложения, чтобы хост
прочитал объявления, и в образе песочницы, чтобы тело работало. Отсюда
правило: **на верхнем уровне модулей, до которых дотягивается манифест,
не должно быть ни одного импорта из `payload`.**

Дотягивается манифест далеко. Хост импортирует `plugin.py`, тот —
`tools.py`, тот — модели профилей и клиентов. Любой верхний импорт по этой
цепочке приезжает в процесс приложения, даже если ни одна его строка там не
выполняется. Так в окружение приложения однажды приехали драйвер
ClickHouse и `numpy` следом за адаптером `pgvector`.

Что делать с тем, что нужно только телу:

```python
"""Инструменты ch: запрос идёт из песочницы."""

from typing import Any

from boba.db.clickhouse import ClickHouseError, ClickHouseQueryError
from boba.toolkit.sql import SqlErrorKind


def _payload() -> Any:
    """Клиент базы: тянет clickhouse-connect, которого в приложении нет.

    Модуль читает хост ради объявлений, а драйвер живёт только в песочнице,
    поэтому импорт отложен до самого вызова.
    """
    from boba.db.clickhouse import payload  # noqa: PLC0415

    return payload.PayloadClickHouse


EXPECTED = {
    ClickHouseError: SqlErrorKind.DATABASE_UNAVAILABLE,
    ClickHouseQueryError: SqlErrorKind.SQL_FAILED,
}
```

Три правила, которые за этим стоят:

- **Импорт тела откладывается до вызова**, с `# noqa: PLC0415` и строкой,
  объясняющей почему. Так же сделано с `liteparse` и `bs4` у `doc` и `kb`.
- **В объявлениях не ссылаются на чужие типы.** Карта `EXPECTED` живёт на
  уровне модуля, значит её ключи обязаны быть своими ошибками: исключение
  драйвера упаковывается на границе пакета клиента. `LiteParseError`
  объявлен в `boba-text`, а бросает его движок из `boba-liteparse`;
  `ClickHouseQueryError` объявлен в `boba-db-clickhouse`, а бросает его
  клиент из его же `payload`-модуля.
- **Пакет-клиент делится надвое.** Модуль `__init__` app-safe части не
  экспортирует клиента, а манифест типа connection импортирует его внутри
  пробы. Иначе связка «entry point → манифест → клиент» притащит драйвер
  на хост при разборе конфига соединений.

Проверяется это одной командой: она импортирует манифест в отдельном
процессе и печатает, попала ли библиотека тела в загруженные модули.

```
.venv/bin/python -c "import sys, boba.tool.ch.plugin; print('clickhouse_connect' in sys.modules)"
```

Ответ `False` — граница цела. Тем же способом ловится и обратное: если
библиотека нужна и приложению, как `psycopg` у слоя данных или `httpx` у
транспорта, она объявляется обычной зависимостью и импортируется как
обычно — прятать её незачем.

---

## 10. Запуск руками и отладка

Тело — обычная программа, и запустить его можно без хоста. Отличие от
запуска launcher'ом только в том, как передаётся injected: вместо
дескриптора `--injected-fd` человек даёт файл `--injected`, а результат
читает из stdout.

| Флаг | Кто передаёт | Зачем |
|---|---|---|
| `--injected <файл>` | человек | JSON с injected-параметрами: тот же объект, что launcher шлёт по дескриптору |
| `--injected-fd <n>` | launcher | дескриптор с тем же JSON |
| `--fd-result <n>` | launcher | куда писать конверт; без него `content` печатается в stdout |
| `--fd-in <порт>=<n>` | launcher | канал входного порта; по флагу на каждый канал, у порта-списка их несколько |
| `--fd-out <порт>=<n>` | launcher | канал выходного порта, так же по флагу на канал |
| `--fd-progress <n>` | launcher | канал отчётов о ходе работы (`ToolProgress`) |
| `--fd-gate <n>`, `--fd-verdict <n>` | launcher | пара пайпов барьера группы (`StreamGroup`); без них `ready()` возвращается сразу |
| `--artifact` | человек | вдобавок к `content` напечатать JSON артефакта |

Профиль connection при ручном запуске подаётся тем же файлом, ключом по
имени параметра:

```bash
cat > /tmp/redis.json <<'EOF'
{
  "source": {"kind": "redis", "host": "redis.corp", "db": 0, "password": "…"},
  "target": {"kind": "redis", "host": "redis-staging.corp", "db": 0, "password": "…"},
  "cfg": {"max_rows": 100, "max_bytes": 1000000, "scan_batch": 200}
}
EOF

.venv/bin/python -m boba.tool.redis.tools redis_copy \
    --source cache --target cache-staging --pattern "cache:*" \
    --injected /tmp/redis.json --artifact
```

Имена `cache` и `cache-staging` в argv тело не использует: профили берутся
из файла. Подсказку по флагам инструмента печатает
`python -m boba.tool.redis.tools redis_copy --help`; в ней только аргументы
модели, injected-параметры в argv не принимаются.

Профиль с keytab так передать нельзя: дамп упадёт «may not leave the
application», потому что билет выпускает приложение. Для отладки
kerberos-connection в файл подставляется готовая секция `kerberos_ticket`
либо профиль с паролем.

Значения контекста подаются тем же файлом: у CLI нет пользователя, и
`boba.runtime.toolcli` из toml их не соберёт, а назовёт параметр, который
нужно подать через `--injected`. Для `send_file` файл выглядит так:

```json
{
  "subject": {"user_id": "00000000-0000-0000-0000-000000000007", "login": "dev", "roles": [], "profile": "general"},
  "scope": {"kind": "chat", "id": "11111111-1111-1111-1111-111111111111"},
  "root": {"path": "/home/dev/workspace"}
}
```

Injected по toml приложения собирает CLI хоста, и под ним же тело можно
запустить под отладчиком. CLI требует оба файла конфигурации — общий
конфиг boba-mcp, в котором есть секции плагинов, и site.toml отладки:

```bash
.venv/bin/python -m boba.runtime.toolcli boba.tool.redis.tools redis_scan \
    --pattern "user:*" \
    --config packages/apps/boba-mcp/conf/config.toml \
    --site debug/mcp/conf/site.toml
```

Проверка обнаружения плагина после установки. Окружение разработчика
готовит `source dev.sh` в корне репозитория: оно ставит `.venv` и
выполняет `uv sync --all-packages --all-extras`. Тесты идут из дерева
отладки и берут файлы конфигурации из переменных, как описано в
руководстве администратора (раздел 8, «Тесты»); долгий прогон запускают в
своей сессии tmux с выводом в файл:

```bash
source dev.sh
cd debug/aid
BOBA_CONFIG_PATH=$PWD/../../packages/apps/boba-aid/conf/config.toml \
BOBA_SITE_PATH=$PWD/conf/site.toml \
    ../../.venv/bin/pytest ../../packages/apps/boba-mcp/tests/test_plugin_discovery.py -q
```

Тесты пишутся интеграционными, на реальных зависимостях, и живут в
пакете плагина (`tests/` рядом с `src/`). Три образца:

- тело как функция: `packages/tools/boba-tool-canvas/tests/test_canvas_tools.py`
  зовёт `tool.coroutine(**kwargs)` над временным workspace, подавая
  `Subject`, `Scope` и `WorkspaceRoot` руками;
- тело над живой базой: `packages/services/boba-connection-broker/tests/test_connection_tools.py`
  кладёт строки и гранты хранилищем брокера и проверяет выдачу под
  разными субъектами;
- путь вызова целиком, от чата до тела: `packages/apps/boba-aid/tests/test_user_connections.py`
  поднимает настоящий сервис boba-mcp с плагинами в песочнице (`ToolService`
  в `aid_stand.py`) и зовёт инструмент портом чата (`ServiceTools`): чат
  запечатывает соединение пользователя, сервис открывает его и исполняет
  тело. Ретранслятор чата между процессом агента и серверами MCP
  проверяет `packages/apps/boba-aid/tests/test_mcp_relay.py`, сам процесс
  агента по ACP поднимает стенд `acp_stand.py`.

---

## 11. Симптомы и причины

- Тип не появился в `kinds()`: не прогнан `uv sync`, или имя entry point
  не совпало с `kind`.
- Старт boba-mcp падает «conf/plugins/<name>.toml is missing»: плагин
  установлен, а файла плагина нет рядом с общим конфигом сервиса
  (`packages/apps/boba-mcp/conf/plugins/`).
- Старт падает «injected parameter 'cfg' annotated …: no config section
  resolves for it: … must define SECTION as the toml section name, got
  None»: забыт `SECTION: ClassVar[str]`.
- Старт падает «… is annotated with …, which is not a connection model» или
  «… whose package is not installed»: параметр с маркером объявлен не
  моделью профиля, либо пакет типа не установлен в этом развёртывании.
- Инструмента с параметром-connection нет в `tools/list`: правила
  `[[mcp.endpoints.<имя>.connections]]` ничего не предложили этому
  вызывающему — ни одно правило не подошло его ролям и клиенту, или
  предложены только соединения сервера, а видимых ему имён в списке
  инструмента нет (раздел 5, «Что предлагается вызывающему»).
- Старт boba-mcp падает «[connections.<имя>]: the profile does not parse as a
  connection of kind …», «… which is not among [connections]» или «…
  server connection … in its list is of kind …»: профиль заданного
  соединения не разбирается своим видом, список инструмента называет
  незаданное соединение или соединение чужого вида.
- Вызов отвергнут «unknown connection 'x': expected one of …» или «sealed
  connections are not offered to this caller, use one of: …»: значение не
  входит в то, что предложено вызывающему на месте параметра.
- Вызов отвергнут «… expired at …; repeat the call with the connection name
  instead of the sealed value»: запечатанное значение старше
  `[agent].seal_ttl_sec`; модель повторяет вызов с именем, и чат
  запечатывает его заново.
- Тело получает `**********` вместо секрета: модель конфига не наследует
  `SecretRevealing`, либо у поля свой `field_serializer`, который маскирует.
- «credentials may not leave the application»: keytab или пароль kerberos
  дошёл до дампа. Профиль не реализует `kerberos_section`,
  `service_name`, `with_call_ticket`, либо это ручной запуск с
  keytab-профилем.
- Connection есть в таблице, а вызов получает «the connection named by …
  characters is not among the connections of kind … available to you»:
  вид строки не тот, что объявлен типом параметра, либо connection не
  выдан ни пользователю, ни его ролям. Имя, выданное дважды разными
  строками, даёт другой отказ: «… matches more than one of your
  connections».
- Зигота секции не поднимается: образ плагина не собран или собран до
  правок деклараций, `make plugin-rootfs PLUGIN=<пакет>`.
- «control closed on call» или смерть зиготы на первом вызове: гость в
  rootfs отстал от хоста, `make plugin-rootfs-all`.
- Библиотека тела приехала в окружение приложения: она объявлена обычной
  зависимостью вместо `payload`, либо импортирована на верхнем уровне
  модуля, до которого дотягивается манифест (раздел 9).
- Тело не видит сеть: нет `network = true` или биндов `resolv.conf` и
  `hosts`.
- «inbound frame does not match the declared port»: источник шлёт `kind`
  вне объявления входа. «raw and framed ports do not mix»: кадровый выход
  соединили с сырым входом.

---

## 12. Отклонения от стандарта и договорённости

Хост и чат держатся стандарта MCP. Ниже — места, где стандарт ничего не
говорит или где сделано иначе; автору инструмента о них нужно знать.

- **MIME диаграмм mermaid — `text/vnd.mermaid`.** Зарегистрированного типа
  у mermaid нет; взят распространённый `text/vnd.mermaid` (суффикс файла
  `.mmd`, член `ContentMime.MERMAID`). По нему чат выбирает вьювер панели и
  ждёт от браузера вердикта отрисовки, поэтому файл диаграммы инструмент
  отдаёт именно с этим MIME — так делает `diagram_save` плагина
  `boba-tool-canvas`.
- **Предела одновременных вызовов в чате нет.** MCP-over-ACP предусматривает
  код `-33000` («превышен предел»), но ретранслятор чата его не выдаёт: все
  вызовы агента уходят серверам. Если предел понадобится в чате, это будет
  очередь, которая принимает все вызовы и выпускает их пачками, без
  отказов. Свой предел держит сервис boba-mcp, секция `[mcp.limits]`:
  `max_runs` вызовов исполняются разом, ещё `max_waiting` ждут места, а
  вызов сверх обоих получает отказ результатом с `isError` («… the service
  already runs … repeat the call later»).
- **Расширение `com.boba/nested-calls`.** Вложенные вызовы стандарт не
  описывает: по схеме инструмента `workflow` соединения в аргументах узлов
  не видны, потому что аргументы узла — свободный объект. Сервер объявляет
  форму связки расширением в своих возможностях (модель
  `NestedCallsFeature` в `boba.toolkit.dag`):

  | Ключ | Значение у boba-mcp | Смысл |
  |---|---|---|
  | `tool` | `workflow` | инструмент со вложенными вызовами |
  | `nodes` | `nodes` | аргумент со списком узлов |
  | `node_key` | `key` | поле узла: ключ узла |
  | `node_tool` | `tool` | поле узла: имя инструмента |
  | `node_args` | `args` | поле узла: аргументы инструмента |
  | `progress_key` | `com.boba/node` | ключ `_meta` уведомления `notifications/progress`, под которым сервер называет узел, чей это отчёт |

  Клиент по объявлению разбирает узлы, запечатывает соединения в
  аргументах каждого узла по схеме его инструмента и собирает вызов
  обратно; узел с полями вне объявленной формы отвергается. Клиент без
  расширения видит `workflow` обычным инструментом.

Остальные расширения сервера — `com.boba/journal` (журнал вызова
ресурсами), `com.boba/files` (файлы workspace) и
`com.boba/connection-seal` (ключ для запечатанных соединений, раздел 5) —
объявляются так же, возможностями сервера; клиент, который их не знает,
работает без них.

- **Вопрос пользователю — форма ACP `elicitation/create`.** Чат объявляет
  агенту формы (`elicitation.form`) и показывает форму панелью вместо поля
  ввода; ответ панели уходит агенту `accept`, Esc и крестик — `decline`,
  вопрос, снятый без пользователя (остановка хода, срок
  `[agent].question_timeout_sec`, закрытая вкладка), — `cancel`. Собственный
  вопрос агента (AskUserQuestion) приходит формой с `toolCallId` вызова:
  поле на вопрос, у вариантов — описание и предпросмотр в `_meta`
  (`boba/preview`), у поля вопроса — ключ парного строкового поля «Other»
  (`_meta` `boba/other`); клиент без этих ключей покажет пару обычным
  текстовым полем. Агент без объявленных форм инструмента вопроса модели
  не предлагает.
- **input_required сервера MCP выполняет агент.** Просьбу сервера о вводе
  (`InputRequiredResult` с формами `elicitation/create`) агент задаёт
  клиенту той же формой ACP с именем сервера и инструмента в `_meta`
  (`boba/server`, `boba/tool`) и повторяет вызов с `inputResponses` и тем
  же `requestState`, но не больше `mcp.input_rounds` раз за вызов. Формы
  серверу агент объявляет в `_meta.clientCapabilities` вызова, только если
  их объявил его клиент; выборку (sampling), корни (roots) и форму-ссылку
  агент не выполняет — вызов кончается ошибкой с причиной. Раунды вопросов
  ложатся в `_meta` результата `tools/call` под ключом `boba/elicitations`
  (модель `ElicitationRound`): по ним лента рисует запись вопроса в шаге
  вызова одинаково вживую и в истории.
