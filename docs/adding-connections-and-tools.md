# Как написать tool-плагин: конфиг, секреты, connection пользователя

Документ ведёт от первого вызова до собранного образа песочницы на одном
сквозном примере. Мы напишем плагин `redis`: сначала инструмент, который
ходит на сервер из конфига администратора, потом тип connection, чтобы
пользователи заводили свои сервера на странице «Connections», потом
инструмент с двумя connection сразу и потоковый инструмент для конвейера.
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
9. Запуск руками и отладка
10. Симптомы и причины

---

## 1. Что происходит, когда модель вызывает инструмент

Приложение (chainlit с чатом или studio с API и страницами) мы дальше
называем **хостом**. У хоста есть набор инструментов, которые он показывает
LLM. Когда модель присылает `tool_call`, хост не исполняет код инструмента
у себя: он собирает команду и запускает её в отдельном процессе. Функция,
которая в этом процессе реально работает, называется **телом** инструмента.

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

Теперь сам путь вызова. Модель прислала `pg_query(connection="analytics",
sql="select 1")`. Хост:

1. проверяет роли пользователя, пишет журнал, ставит отмену по кнопке;
2. подкладывает в аргументы вызова то, чего модель не присылала:
   конфиг секции `[tool.pg]` и профиль connection `analytics` из таблицы
   connections, с билетом kerberos для этого вызова;
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
  получает готовые модели и работает с ними. Вся политика решена хостом до
  запуска.

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

Пакет добавляется в `members` корневого `pyproject.toml` рядом с соседями.

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
| connection пользователя | `connection: Annotated[RedisConnection, UserConnection]` | строка таблицы connections по имени от модели | `UserConnections` на вызове |
| субъект вызова | `subject: Annotated[Subject, Injected]` | `CallContext`: id пользователя, логин, роли, профиль | `CallContextValues` на вызове |
| область вызова | `scope: Annotated[Scope, Injected]` | `CallContext`: тред чата, запуск workflow или задание | `CallContextValues` на вызове |
| корень workspace | `root: Annotated[WorkspaceRoot, Injected]` | профиль запуска: `/workspace` в песочнице, `workdir` в `process` | `CallContextValues` на вызове |
| порт данных | `out: Annotated[Outbound[...], Injected]` | канал кадров графа workflow | песочница на вызове (раздел 7) |

Все injected-значения, кроме портов, уезжают телу одним JSON по
дескриптору, ключ — имя параметра. Модели контекста живут в core:
`Subject` и `Scope` в `boba.identity.context`, `WorkspaceRoot` в
`boba.canvas.keys`; список типов, которые хост умеет подставлять, — это
`CallContextValues.SOURCES` в `boba.toolrun.callvalues`. Новый вид
значения добавляется туда же одной строкой, а не обвязкой в плагине.

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
ленте и странице, как показывать значение аргумента (блок кода с
подсветкой); на модель и на тело она не влияет.

Connection и порты появятся в разделах 5 и 7. Сейчас нужны первые два.

Контекст вызова нужен телу, которое само считает права по таблицам
(`Subject` — так устроен `connection_list`, раздел 5) или работает с
файлами треда (`Scope` и `WorkspaceRoot`). Так выглядит `send_file` плагина
`boba-tool-canvas`:

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
    except CanvasRefusedError as e:
        return e.result()

    return FileResult(path=key.in_workspace(), name=key.name, mime=ThreadFiles.mime_of(key))
```

`ThreadFiles` зовёт `root.apply()`, и после этого `ObjectKey.from_workspace`
принимает только пути своего треда: `/workspace/<thread_id>/upload/...` в
песочнице, `<workdir>/<thread_id>/upload/...` в режиме `process`. Всё, что
тело делает с чатом, умещается в возврате `FileResult`: вложение к шагу
прикрепляет хост.

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
старт падает с текстом «injected parameter 'cfg' has no SECTION on its
model».

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
класс. По ней хост знает, как показать результат в чате и на странице
workflow: `llm_view()` — текст модели, `chat_view()` — markdown шага и
`items` для поверхности чата (`VisualElement`, `FileElement`, `PanelOpen`),
`studio_view()` — блоки страницы.

Тело ничего не знает про чат: файл оно пишет в смонтированный workspace и
возвращает результат. Элементы монтирует обвязка чата `ChatMount` после
тела: вложение — строкой элемента и показом через шину хода, панель —
содержимым вьювера плюс ссылкой в переписке. Если вьювер браузера ответил,
что файл не отрисовался (mermaid), обвязка подменяет результат на
`ErrorResult` с вердиктом — так отказ доходит до модели тем же путём, что
любой отказ инструмента. Образец — плагин `boba-tool-canvas`
(`canvas_open`, `send_file`, `diagram_save`, секция `[tool.canvas]` с
`workspace = true`). Телу, которому нужен корень workspace или тред вызова,
хост подаёт их injected-параметрами `root: Annotated[WorkspaceRoot, Injected]`
и `scope: Annotated[Scope, Injected]`.

**`EXPECTED`** — карта «исключение → вид отказа». Исключение из этой карты
уезжает конвертом `ReplyError` и показывается пользователю текстом. Любое
другое исключение считается дефектом: трейсбек в stderr, код выхода не
ноль, хост поднимает `LauncherError` с хвостом stderr.

**`TOOLS` и блок `__main__`** делают модуль программой. Одну и ту же
команду `python -m boba.tool.redis.tools redis_scan --pattern "user:*"`
исполняет launcher и человек в терминале.

### Манифест и pyproject

```python
"""Манифест плагина redis: entry point группы boba.tools."""

from typing import Final

from boba.tool.redis.tools import TOOLS
from boba.toolkit.manifest import ToolPluginManifest

MANIFEST: Final = ToolPluginManifest(section="redis", tools=tuple(TOOLS))
```

```toml
[project]
name = "boba-tool-redis"
version = "0.0.17.dev3"
dependencies = ["boba-toolkit==0.0.17.dev3"]

[project.entry-points."boba.tools"]
redis = "boba.tool.redis.plugin:MANIFEST"

[project.optional-dependencies]
payload = ["redis>=5"]

[tool.boba.sandbox]
imports = ["redis"]
```

Группа **`payload`** — зависимости тела. Хост их у себя не ставит и не
импортирует: клиент redis нужен только внутри процесса тела. Так каждый
плагин остаётся самостоятельной программой со своими зависимостями, которую
можно запустить и проверить без LLM.

Секция **`[tool.boba.sandbox]`** нужна сборщику образа песочницы; в рантайме
она не читается. Ключ `imports` перечисляет модули, которые сборщик
попробует импортировать внутри готового образа: если `redis` в образ не
попал, сборка упадёт здесь, а не на первом вызове в проде. Остальные ключи
секции описаны в разделе 8.

Entry point материализуется установкой: после правки pyproject нужен
`uv sync --all-packages`.

### Файл конфига

Хост не стартует, пока для установленного плагина нет файла
`conf/plugins/redis.toml`. Файл кладётся в каждое развёртывание, где плагин
установлен: `compose/chainlit/conf/plugins/` и `compose/studio/conf/plugins/`.

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
- `headless` — подмножество `tools`, которое модели в чате не отдаётся:
  такие инструменты зовут страница, REST и задачи workflow. Живой пример:
  `pg_schema_snapshot` в `pg.toml` снимает снимок каталога по задаче
  синхронизации.
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

В файл плагина пароль напрямую не пишется: конфиги плагинов одинаковы во
всех развёртываниях. Секрет лежит в основном `config.toml`, а файл плагина
ссылается на него интерполяцией:

```toml
[server]
    host     = "redis.corp"
    port     = 6379
    db       = 0
    password = "${site.redis_password}"
```

Целую таблицу можно подключить одной ссылкой: `server = "${site.redis}"`
возьмёт таблицу `[site.redis]` из `config.toml` вместе с паролем. Так
сделано у плагина `kb`: `connection = "${postgres}"`.

Так устроен `KbToolConfig` плагина `kb`: модель наследует
`SecretRevealing`, а внутри лежит целый `PostgresConfig` с паролем.

---

## 4. Тип connection: свой connection у каждого пользователя

Сервер в конфиге администратора — это один сервер на всех. Обычно нужно
иначе: пользователь заводит свой redis на странице «Connections», выдаёт его
себе или роли, и модель по имени выбирает, куда идти. Для этого хост хранит
connection в таблице `connections`: id, имя, описание и `data jsonb` с
профилем, секретная часть которого зашифрована ключом из секции
`[connections]` конфига. Таблицы `roles` и `grants` описывают, кому какой
connection выдан.

Чтобы хост умел разбирать строку таблицы в модель, у каждого профиля есть
поле `kind`. По нему реестр типов connection, собираемый из entry points
группы `boba.connections`, находит модель профиля и функцию пробы. Проба —
это кнопка «Check» на странице connections.

Тип connection и плагин инструментов — разные вещи с разными entry points.
Их можно положить в один пакет или в два. Postgres, clickhouse и web
сделаны двумя: тип живёт в инфра-пакете (`packages/infra/db/boba-db-postgres`),
инструменты в `packages/tools/boba-tool-postgres` и зависят от него. Так
нужно, когда типом пользуется несколько плагинов или когда тип нужен
studio отдельно от инструментов. Для redis типом пользуется только наш
плагин, поэтому объявим оба entry point в одном пакете:

```
packages/tools/boba-tool-redis/
    pyproject.toml
    src/boba/tool/redis/
        profile.py       # модель профиля
        connection.py    # MANIFEST типа    -> boba.connections
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

Профиль наследует `ConnectionProfileBase` из `boba.connections.base`.
Базовый класс даёт поля `kind`, `description` и `source`, а от наследника
требует метод `trace()`; остальные методы переопределяются по
необходимости.

```python
"""Профиль connection redis."""

from __future__ import annotations

from typing import ClassVar, Literal

from pydantic import Field, SecretStr

from boba.connections.base import ClientIdentity, ConnectionProfileBase


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


class RedisConnection(ConnectionProfileBase):
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
  его администратор на странице connections.
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

from boba.connections.base import ConnectionProfileBase, ConnectionTypeError
from boba.connections.manifest import ConnectionTypeManifest
from boba.tool.redis.profile import RedisConnection

__all__ = ["MANIFEST"]


async def _probe(profile: ConnectionProfileBase) -> str:
    if not isinstance(profile, RedisConnection):
        msg = f"redis probe expects a RedisConnection profile, got kind {profile.kind!r}"
        raise ConnectionTypeError(msg)

    client = Redis(
        host=profile.host,
        port=profile.port,
        db=profile.db,
        password=profile.password.get_secret_value(),
    )
    try:
        pong = await client.ping()
    finally:
        await client.aclose()

    return f"PONG {pong}"


MANIFEST = ConnectionTypeManifest(kind="redis", profile=RedisConnection, probe=_probe)
```

Исключения пробы глотать не надо: граница превратит их в результат
«проверка не прошла» с текстом.

После `uv sync --all-packages` тип виден реестру:

```bash
.venv/bin/python -c "from boba.connections.manifest import ConnectionTypes; print(ConnectionTypes.discover().kinds())"
```

Страница «Connections» покажет новый тип сама: форма строится из json-schema
модели профиля. Если пакет типа удалить, строки его вида в списках получат
пометку «type not installed».

Образец в репозитории: `PostgresConfig` в пакете `boba-db-postgres`, где
проба выполняет `select version()` и возвращает версию сервера.

---

## 5. Инструмент с connection пользователя

Теперь перепишем инструменты так, чтобы сервер приходил из таблицы
connections, а не из конфига. Connection объявляется прямо в подписи: тип
параметра — модель профиля, маркер `UserConnection` рядом:

```python
from boba.toolkit.facade import Injected, UserConnection, tool

RedisTarget = Annotated[RedisConnection, UserConnection]
```

У такого параметра две стороны. Для модели это строка: имя connection,
которое она выбирает по выдаче `connection_list`. Хост правит схему при
загрузке, и вместо модели профиля модель видит строку с подсказкой. Для
тела это готовый профиль с паролем внутри. Ничего регистрировать не нужно:
вид connection хост выводит из типа параметра через реестр типов.

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
прав, whitelist'а. Всё это осталось на хосте. Конфиг секции при этом
никуда не делся: в нём живут лимиты и настройки администратора, а
`server` из него теперь можно убрать.

Два требования к такому инструменту. Он обязан быть `async def`: хост ждёт
таблицу и билет, синхронный вызов падает `InjectedAsyncOnlyError`. И
инструменты с connection работают только при `[connections] enable = true`
в `config.toml`, иначе старт падает с текстом «takes its connections from
the connections table».

Манифест и файл плагина остаются обычными. Профилей в файле нет: они
приходят из таблицы на каждый вызов.

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

Инструменты `connection_list` и `connection_search` — обычный плагин
`boba-tool-connections` (секция `[tool.connections]`, файл
`conf/plugins/connections.toml`). Тело исполняется в песочнице как любое
другое и читает таблицы
connections/roles/grants приложения своим подключением из конфига
(`connection = "${postgres}"`, `db_schema`). Кто спрашивает, тело узнаёт
из injected-параметра `subject: Annotated[Subject, Injected]`: хост
подставляет субъект вызова обвязкой `CallContextValues`, как соединения — обвязкой
`UserConnections`. Выдача `connection_list` — все connection, доступные
пользователю лично или любой его роли:

| connection | kind | host | description |
|---|---|---|---|
| `cache` | `redis` | `redis01.corp` | кэш сессий прода, только чтение |
| `cache-staging` | `redis` | `redis-stg.corp` | кэш стейджинга, можно писать |
| `analytics` | `postgres` | `dwh01.corp` | витрины продаж |

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
(`boba.connections.grants`) отдаёт текст SQL и параметры, а брокер на вызове
и тело инструмента лишь подставляют идентификаторы своих схем
(`SqlNames.mapping` по картам `ConnectionNames`) и исполняют его своим
подключением. Запрос считает `copies` — сколько строк субъекта носят имя
внутри вида — по всем выданным строкам, а фильтры `ConnectionFilter`
отбирают строки уже из них. Имя-дубль на вызове даёт
`AmbiguousConnectionError`, в выдачу инструментов не попадает
(`unique_only`), и фильтр поиска сделать его уникальным не может.

### Что происходит на вызове

Модель вызвала `redis_copy(source="cache", target="cache-staging",
pattern="cache:*")`.

1. На загрузке хост нашёл у `redis_copy` два параметра с маркером и вывел
   их вид из типа: `redis`. В схеме для модели на их месте строки.
2. На вызове хост читает из аргументов имена `cache` и `cache-staging`.
3. Берёт субъект вызова из контекста: пользователь, логин, роли.
4. Одним SQL читает гранты на пользователя и его роли с фильтром по виду
   `redis` и группирует по имени. Имя, выданное дважды с разными строками
   (лично и ролью), попадает в список неоднозначных.
5. Выбирает строку по каждому имени. Нет такой — отказ с перечнем
   доступных имён, чтобы модель исправилась и повторила вызов. Дубль —
   отказ `ambiguous_connection`.
6. Зовёт `profile.labeled(client)`: профиль подписывает сессию сам.
7. Для kerberos-типов меняет kerberos-секцию на билет вызова (раздел 6).
8. Имена остаются строками в argv, профили уезжают JSON-каналом под
   ключами `"source"` и `"target"`.
9. Тело собирает JSON обратно в два `RedisConnection` и работает.

В тело уезжают ровно те профили, которые назвал вызов. Остальные connection
пользователя туда не попадают, даже именами.

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
    cfg: Annotated[WebGrepConfig, Injected],
) -> MarkdownResult:
    """Скачивает URL соединением connection (см. connection_list)."""
    profile = WebHost.bound(connection, url)
```

`WebHost.bound` из `boba.transport.http.web` проверяет покрытие и
возвращает профиль, привязанный к конкретному хосту. Чужой хост —
`UnknownHostError`, объявленный в `EXPECTED` модуля как `unknown_host`.

Образец в репозитории: плагин `pg` объявляет
`PgConnection = Annotated[PostgresConfig, UserConnection]` и строит на нём
все семь инструментов. Хостовая сторона, которая ищет строку и подкладывает
профиль, живёт в пакете `boba-connection-broker`.

---

## 6. Kerberos: что тип обязан уметь

Redis kerberos не умеет, поэтому этот раздел про postgres и web. Строка
таблицы может нести kerberos-секцию трёх видов:

- `{method = "kerberos_delegated"}`: в сервис идёт сам пользователь.
  Работает, если он вошёл через SSO и браузер делегировал креды. Иначе
  отказ `no_delegated_credentials`.
- `{method = "kerberos_keytab", principal, keytab}`: сервисная учётка,
  keytab лежит на хосте приложения.
- `{method = "kerberos_password", principal, password}`: то же, но паролем.

Ни keytab, ни пароль в тело не уезжают. Хост выпускает **один сервисный
билет** к конкретному сервису и подменяет секцию профиля на
`{method = "kerberos_ticket", ccache}` с этим билетом в base64. Форма
профиля не меняется, и тело не знает, как билет получен. TGT в ccache нет,
выпустить билет к другому сервису тело не может.

Чтобы хост мог это сделать, не зная устройства профиля, профиль реализует
три метода базового класса. Так они выглядят у `PostgresConfig`:

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
в postgres или файл? Для этого есть граф workflow (страница и API studio):
узлы графа — инструменты, и данные текут между ними через ядро, не проходя
ни через модель, ни через хост.

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

- Не больше одного входного и одного выходного порта на инструмент.
  Несколько видов кадров — объединение моделей в одном порте. Кадр с
  `kind` вне объявления роняет вызов на границе, до тела.
- `item.head` — модель заголовка, `item.body` — `memoryview` на буфер
  кадра; для склейки нужен `bytes(item.body)`.
- `RawInbound` и `RawOutbound` — голые байты без кадров, для перекачки
  вроде `COPY ... TO STDOUT` → `COPY ... FROM STDIN`. Сырое совместимо
  только с сырым, стыковку с кадровым портом конвейер отвергает до старта.
- Запись в порт блокируется при медленном потребителе: залить хост тело
  не может.
- `return` остаётся: конверт — итог, кадры — то, что по дороге.
- Порты не снимаются со схемы, но обязательными быть не могут: их значение
  строит песочница на вызове.

Регистрировать узел не нужно: порт в подписи уже делает инструмент узлом
каталога `GET /v1/tools`, стыковку по объявленным `kind` проверяет
движок workflow до запуска.

Образец сырых портов в репозитории: `pg_copy_out` и `pg_copy_in` плагина
`pg`, которые гонят `COPY` между двумя базами байт в байт.

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

Секцию читает `make -C build/<app> plugin-rootfs PLUGIN=<пакет>`.
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
хоста по протоколу каналов. У chainlit и studio песочницы свои:
`build/chainlit` и `build/studio`.

### Прогрев зиготы

Если тело на каждом вызове поднимает что-то тяжёлое (модель ONNX, словарь),
это можно сделать один раз в зиготе, и форки получат результат через
copy-on-write. Для этого объявляется корутина с декоратором `@warmup` и
единственным параметром-моделью конфига. Так делает `kb`:

```python
@warmup
async def warm_embedder(cfg: KbWarmupConfig) -> None:
    """Модель ONNX поднимается в зиготе: дети берут её через COW."""
    embedder = WarmEmbedder.load(cfg.embedding)
    await embedder.embed_query("warm-up")
```

Конфиг хука хост собирает из той же секции `[tool.<секция>]` с раскрытыми
секретами. В режиме `process` прогрев не действует.

---

## 9. Запуск руками и отладка

Тело — обычная программа, и запустить его можно без хоста. Отличие от
запуска launcher'ом только в том, как передаётся injected: вместо
дескриптора `--injected-fd` человек даёт файл `--injected`, а результат
читает из stdout.

| Флаг | Кто передаёт | Зачем |
|---|---|---|
| `--injected <файл>` | человек | JSON с injected-параметрами: тот же объект, что launcher шлёт по дескриптору |
| `--injected-fd <n>` | launcher | дескриптор с тем же JSON |
| `--fd-result <n>` | launcher | куда писать конверт; без него `content` печатается в stdout |
| `--fd-frames <n>` | launcher | канал кадров портов |
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

Injected по toml приложения собирает CLI хоста, и под ним же тело идёт под
отладчиком (цель «pg_query tool» в `launch.json`):

```bash
.venv/bin/python -m boba.runtime.toolcli boba.tool.redis.tools redis_scan \
    --pattern "user:*" --config compose/chainlit/conf/config.toml
```

Проверка обнаружения плагина после установки:

```bash
./build/chainlit/src/uv/uv sync --all-packages
cd compose/chainlit && BOBA_TOOL_LAUNCHER=process ../../.venv/bin/python -m pytest \
    ../../packages/services/boba-runtime/tests/test_plugin_discovery.py -q
```

Тесты пишутся интеграционными, на реальных зависимостях, и живут в
пакете плагина (`tests/` рядом с `src/`). Три образца:

- тело как функция: `packages/tools/boba-tool-canvas/tests/test_canvas_tools.py`
  зовёт `tool.coroutine(**kwargs)` над временным workspace, подавая
  `Subject`, `Scope` и `WorkspaceRoot` руками;
- тело над живой базой: `packages/tools/boba-tool-connections/tests/test_connection_list.py`
  кладёт строки и гранты хранилищем брокера и проверяет выдачу под
  разными субъектами;
- тело под обвязками хоста: `packages/agents/boba-chainlit/tests/test_user_connections.py`
  собирает инструмент как загрузчик (`ToolBridge.as_structured_tool`,
  `CallContextValues.bind_all`, `UserConnections.bind_all`,
  `InjectedConfig.bind_all`) и зовёт его через зиготу секции; для чата
  так же ставится `ChatMount` (`test_chat_mount.py`).

---

## 10. Симптомы и причины

- Тип не появился в `kinds()`: не прогнан `uv sync`, или имя entry point
  не совпало с `kind`.
- Старт падает «conf/plugins/<name>.toml is missing»: плагин установлен,
  файла в развёртывании нет. Проверить и studio.
- Старт падает «injected parameter 'cfg' has no SECTION on its model»:
  забыт `SECTION: ClassVar[str]`.
- Старт падает «takes its connections from the connections table»: у
  инструментов есть параметры-connection при `[connections] enable = false`.
- Старт падает «is not a connection profile» или «package is not
  installed»: параметр с маркером объявлен не моделью профиля, либо пакет
  типа не установлен в этом развёртывании.
- Тело получает `**********` вместо секрета: модель конфига не наследует
  `SecretRevealing`, либо у поля свой `field_serializer`, который маскирует.
- «credentials may not leave the application»: keytab или пароль kerberos
  дошёл до дампа. Профиль не реализует `kerberos_section`,
  `service_name`, `with_call_ticket`, либо это ручной запуск с
  keytab-профилем.
- «is built in the async body only»: инструмент с connection или
  контекстом (`Subject`, `Scope`, `WorkspaceRoot`) объявлен `def`, а не
  `async def`.
- «this tool works only inside a chat turn»: результат несёт `PanelOpen`
  или `FileElement`, а вызов пришёл не из хода чата. Само тело отработало;
  вне чата (REST, workflow) такие элементы монтировать некому, и тул там
  показывает лишь `studio_view()`.
- Connection есть в таблице, а вызов получает «not available to you»: имя
  выдано дважды и попало в неоднозначные, либо вид строки не тот, что
  объявлен типом параметра.
- Зигота секции не поднимается: образ плагина не собран или собран до
  правок деклараций, `make plugin-rootfs PLUGIN=<пакет>`.
- «control closed on call» или смерть зиготы на первом вызове: гость в
  rootfs отстал от хоста, `make plugin-rootfs-all` для обеих сборок.
- Тело не видит сеть: нет `network = true` или биндов `resolv.conf` и
  `hosts`.
- «inbound frame does not match the declared port»: источник шлёт `kind`
  вне объявления входа. «raw and framed ports do not mix»: кадровый выход
  соединили с сырым входом.
