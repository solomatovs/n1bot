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
13. Спецификация плагина, версия 1

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
инструмента у себя: он собирает аргументы и запускает отдельный процесс.
Функция, которая в этом процессе реально работает, называется **телом**
инструмента.

Тело живёт в pip-пакете, который мы называем **плагином**. У каждого
плагина одно короткое имя, **id**: `pg`, `doc`, `web`. По id сервер находит
комплект плагина — образ песочницы `sandbox/plugins/<id>.ext4` и файл
конфига `conf/plugins/<id>.toml`. Конфиг экземпляра сервиса подключает
каталог плагинов ключом `tool = "${toml_dir:plugins}"`, и файл становится
таблицей `[tool.<id>]` того же дерева. Поэтому внутри файла плагина работают
ссылки на секции конфига экземпляра: `${app.schema}`, `${postgres}`,
`${redis}`.

Кода плагина сервер не импортирует. В корне образа лежит программа
плагина `/plugin/run` с подкомандами `manifest`, `setup`, `zygote` и
`call`. На старте сервер зовёт `manifest` и получает от программы
**манифест** — JSON с инструментами, их схемами и тем, что нужно телу
(раздел 13). Дальше сервер работает только с этими данными: строит
инструменты для MCP, ставит обвязки, готовит каналы. Программу пишет не
автор плагина: её даёт SDK, а автор объявляет плагин объектом
`ToolPlugin` (раздел 2), и программа сама строит манифест из этого
объявления.

Как тело запускается, решает секция `[tool_launcher]` конфига приложения.
Есть два способа:

- `provider = "process"`: обычный субпроцесс хоста — программа плагина
  `python -m boba.sandbox.program <id>` из окружения разработчика.
  Используется в разработке и под отладчиком. Собранный boba-mcp (бинарник
  Nuitka, python в нём нет) этот способ отвергает на старте.
- `provider = "sandbox"`: тело исполняется внутри изолированного контейнера
  на bwrap, корнем служит образ плагина `<id>.ext4`. Так работает релиз.
  Чтобы не платить за старт python и импорт тяжёлых библиотек на каждом
  вызове, для каждого плагина при старте приложения поднимается
  **зигота**: процесс `/plugin/run zygote` внутри песочницы, который уже
  всё импортировал и ждёт. Вызов инструмента становится форком зиготы.

Теперь сам путь вызова. Модель попросила `pg_query(connection="analytics",
sql="select 1")`. По дороге к хосту чат заменил имя `analytics` профилем
этого соединения пользователя, запечатанным ключом хоста (раздел 5), а в
параметр `scope_id` поставил область, из которой пришёл вызов. Хост:

1. проверяет роли пользователя, пишет журнал, ставит отмену по кнопке;
2. подкладывает в аргументы вызова то, чего модель не присылала:
   секцию `[tool.pg]` и профиль connection — открытый запечатанный
   профиль от чата либо заданное соединение сервера, если модель назвала
   его имя, — с билетом kerberos для этого вызова. Таблицу соединений
   хост не читает: она у чата;
3. собирает аргументы модели и подложенные значения в один JSON-объект,
   ключ — имя параметра тела, и отправляет его телу по файловому
   дескриптору; в argv тела только имя инструмента и номера каналов;
4. отдаёт вызов launcher'у.

Тело делает обратное: проверяет JSON моделью вызова, собирает
типизированные значения, зовёт функцию инструмента и пишет ответ
**конвертом** в другой дескриптор. Конверт бывает двух видов: `ReplyOk` с
результатом и `ReplyError` с видом отказа и текстом для пользователя.
Аргументы модели хост не проверяет: непригодный аргумент тело отвергает
конвертом, и в тексте названо каждое непригодное поле.

Из этого пути следуют три правила, которые объясняют всё дальнейшее:

- Модель видит в схеме только те параметры, которые сама заполняет.
  Служебных параметров в схеме нет: модель их не видит и подделать не
  может.
- Всё идёт каналом аргументов, а не argv: argv виден в `ps`, в журнале и в
  трейсбеке, а секреты и большие значения туда попадать не должны.
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
        plugin.py      # объявление плагина ToolPlugin: entry point boba.tools
        tools.py       # конфиг, инструменты, TOOLS
```

Больше нигде пакет не объявляется: `members` корневого `pyproject.toml`
берёт `packages/tools/*` глобом, а сборка находит плагины по entry points
группы `boba.tools` (`scripts/plugin_rootfs.py ids`): имя entry point'а —
id плагина, и `make all` собирает образ песочницы `<id>.ext4` на каждый.
Сборка образа boba-mcp ставит пакет и в окружение сервиса, но сервер его
код не импортирует (это проверяет `test_no_plugin_code.py` пакета
boba-mcp). В образ чата плагины не ставятся.

### Параметры инструмента: модель заполняет одни, хост — остальные

Инструмент — это `async def` с декоратором `@tool` из
`boba.toolkit.facade`. Модель заполняет только аргументы с `Field(...)`:
они попадают в схему инструмента в манифесте (`input_schema`). Всё
остальное подкладывает хост, и таких **предопределённых
injected-параметров** ровно шесть видов. Принцип один: параметр
объявляется аннотацией, построитель манифеста узнаёт его по типу или
маркеру и называет в манифесте его роль, а хост по роли подставляет
значение на каждом вызове; модель этих параметров не видит и подделать не
может. Объявить любой из них может любой инструмент любого плагина —
никакой регистрации, кроме самой аннотации.

| Параметр | Аннотация | Откуда значение | Кто подставляет |
|---|---|---|---|
| аргумент модели | `pattern: Annotated[str, Field(...)]` | LLM | — (ключ `"pattern"` JSON аргументов) |
| конфиг плагина | `cfg: Annotated[RedisToolConfig, Injected]` | таблица `[tool.<id>]` целиком, роль `config` | `InjectedConfig` |
| connection | `connection: Annotated[RedisConnection, UserConnection]` | запечатанный профиль соединения пользователя от чата или заданное соединение сервера | `SealedConnectionParam` (`boba-connection-broker`) на вызове |
| субъект вызова | `subject: Annotated[Subject, Injected]` | `CallContext`: id пользователя, логин, роли, профиль — из токена вызывающего | `CallContextValues` на вызове |
| область вызова | `scope: Annotated[Scope, Injected]` | `CallContext`: область из параметра `scope_id` с пометкой `x-boba-scope`; без него — личный workspace пользователя | `CallContextValues` на вызове |
| корень workspace | `root: Annotated[WorkspaceRoot, Injected]` | профиль запуска: `/workspace` в песочнице, `workdir` в `process` | `CallContextValues` на вызове |
| порт данных | `out: Annotated[Outbound[...], Injected]` | канал кадров графа workflow | песочница на вызове (раздел 7) |

Все значения, кроме портов, уезжают телу одним JSON по дескриптору
вместе с аргументами модели, ключ — имя параметра. Модели контекста живут
в core и наследуют `ContextValue` из `boba.toolkit.manifest`: `Subject` и
`Scope` в `boba.identity.context`, `WorkspaceRoot` в `boba.canvas.keys`;
каждая называет свою роль `ROLE` (`subject`, `scope`, `workspace_root`),
а `CallContextValues` в `boba.toolrun.callvalues` подставляет значение по
роли. Любая другая модель с маркером `Injected` — роль `config`: её
значение — секция плагина. Новый вид значения добавляется ролью в
`InjectedRole`, а не обвязкой в плагине.

Параметр `scope_id` хост сам ставит в схему каждого своего инструмента
(`CallFields.scoped` в `boba.toolrun.call_id`): необязательная строка с
пометкой `x-boba-scope`. Наш чат прячет его от модели и подставляет
область вызова, чужой клиент MCP показывает модели, и она заполняет его
сама или работает в личном workspace. Телу этот параметр не уходит: по
нему хост выбирает область вызова. Имя поля хоста, совпавшее с аргументом
модели, — отказ старта.

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

`ScopeFiles` зовёт `root.apply()` и строит ключ через `CanvasScope.key` из
`boba-canvas`, а тот принимает только пути своей области: `/workspace/<scope_id>/upload/...` в
песочнице, `<workdir>/<scope_id>/upload/...` в режиме `process`. Всё, что
тело делает с чатом, умещается в возврате `FileResult`: хост отдаёт его
ссылкой на файл, а чат показывает её вложением к шагу.

### Модель конфига и тело

```python
"""Инструменты redis: функции уровня модуля.

Ошибки:
RedisError — сервер недоступен или отверг команду.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated, Final

from pydantic import BaseModel, ConfigDict, Field
from redis.asyncio import Redis
from redis.exceptions import RedisError

from boba.toolkit.entry import ToolMain
from boba.toolkit.facade import Injected, tool
from boba.toolkit.result import TableResult
from boba.toolkit.sql import QueryRowLimit, SqlErrorKind, SqlLimits


class RedisServer(BaseModel):
    """Адрес сервера из конфига администратора."""

    host: str = Field(min_length=1)
    port: int = Field(default=6379, ge=1)
    db: int = Field(default=0, ge=0)


class RedisToolConfig(SqlLimits):
    """Сервер и лимиты выдачи; секция [tool.redis]."""

    model_config = ConfigDict(extra="ignore")

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
                "или 'session:2026-*'."
            ),
        ),
    ],
    limit: QueryRowLimit,
    cfg: Annotated[RedisToolConfig, Injected],
) -> TableResult:
    """Перебрать ключи по шаблону с типом и TTL каждого."""
    window = cfg.window(0, limit)
    client = Redis(host=cfg.server.host, port=cfg.server.port, db=cfg.server.db)

    rows: list[dict[str, object]] = []
    try:
        async for key in client.scan_iter(match=pattern, count=cfg.scan_batch):
            if len(rows) >= window.limit:
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
```

Разберём, что здесь за что отвечает.

**Модель конфига** параметра `cfg` получает таблицу `[tool.redis]`
целиком: хост кладёт её значения в JSON аргументов под ключом `"cfg"`, то
есть под именем параметра, а тело разбирает их этой моделью. Своей модели
у хоста нет: проверяет секцию сам плагин подкомандой `manifest` на старте
сервера, и секция, которая не проходит модель, роняет старт текстом
«[tool.redis] does not fit RedisToolConfig: …».

**`extra = "ignore"`** нужен потому, что в той же таблице toml лежат
служебные ключи `enable`, `tools`, `connections`, `sandbox`. Их читает
загрузчик хоста, а не ваша модель.

**`SqlLimits`** из `boba.toolkit.sql` даёт обязательные поля `max_rows` и
`max_bytes` — потолки одного ответа, которые администратор задаёт в секции
плагина. Модель сама просит окно аргументом `limit` (тип `QueryRowLimit`: его
описание говорит модели о потолке), а `cfg.window(offset, limit)` отказывает
`ToolRefusalError` с числом потолка, если `limit` больше `max_rows`;
`cfg.checked(result)` так же отказывает ответу длиннее `max_bytes` байт текста
для модели. Молча выдача не обрезается. Инструменту без таблиц подойдёт обычный
`BaseModel`.

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

**`TOOLS`** — кортеж инструментов модуля; его перечисляет объявление
плагина. Своего входа у модуля нет: тело запускает программа плагина,
подкоманда `call redis_scan` (раздел 10).

### Объявление плагина и pyproject

Объявление плагина — объект `ToolPlugin` из `boba.toolkit.manifest`. Из
него программа плагина строит манифест, который читает сервер:

```python
"""Объявление плагина redis: entry point группы boba.tools."""

from typing import Final

from boba.tool.redis.tools import TOOLS
from boba.toolkit.manifest import ToolPlugin

PLUGIN: Final = ToolPlugin(id="redis", tools=tuple(TOOLS), network=True)
```

Поля объявления:

- `id` — единственное имя плагина: секция `[tool.<id>]`, файлы комплекта
  `<id>.ext4` и `<id>.toml`, имя entry point'а.
- `tools` — инструменты фасада `@tool`.
- `network` — телу нужна сеть: сервер даёт песочнице сеть хоста и
  кладёт в неё `/etc/resolv.conf`, `/etc/hosts` и `krb5.conf` из
  `[env].krb`.
- `data` — образы данных: `DataImage(name="fastembed",
  target="/var/cache/fastembed")` монтирует read-only образ
  `<models>/fastembed.ext4` программы сервера в точку `target`.
- `setup` — хранилище плагина: `PluginSetup(KnowledgeBaseSchemaConfig,
  KbSchema)` — модель секции и фабрика хранилища с методом `setup()`;
  сервер зовёт подкоманду `setup` на старте (раздел 13).
- `warmups` — прогревы зиготы, объявленные `@warmup` (раздел 8).

Плагины `confluence` и `ingest` — два объявления в одном пакете
(`boba.tool.confluence.plugin:CONFLUENCE` и `:INGEST`): у каждого свой
id, своя секция и свой образ.

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
redis = "boba.tool.redis.plugin:PLUGIN"

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
- **`entry-points`** — имя группы `boba.tools`, ключ равен id плагина,
  значение указывает на объявление `PLUGIN`. По нему программа плагина
  находит своё объявление в своём процессе: `python -m
  boba.sandbox.program redis ...`. Сервер entry points плагинов не читает.
- **`[tool.boba.sandbox]`** — декларации сборщика образа; ключ `imports`
  перечисляет модули смоук-проверки после сборки: если `redis` в образ не
  попал, сборка упадёт здесь, а не на первом вызове в проде. Остальные
  ключи секции в разделе 8.

Entry point материализуется установкой: после правки pyproject нужен
`uv sync --all-packages --all-extras`.

### Файл конфига

Файл `conf/plugins/redis.toml` и образ `sandbox/plugins/redis.ext4` —
комплект плагина: конфиг без образа и образ без конфига не дают серверу
стартовать. Файл плагина лежит в git:
`packages/apps/boba-mcp/conf/plugins/redis.toml`. Сборка кладёт его в образ
сервиса (`/app/boba/conf/plugins`), а отладка подключает каталог пакета прямо
(`tool = "${toml_dir:../../../packages/apps/boba-mcp/conf/plugins}"` в
`debug/mcp/conf/config.toml`). Значения машины — адреса, логины, секреты —
живут в `config.toml` экземпляра, а файл плагина на них ссылается.

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
    [sandbox.limits]
        group_memory_bytes = 1073741824
```

- `enable = false` выключает секцию целиком: плагин не загружается.
- `tools` — список инструментов, которые получит модель. Инструмент есть
  в `TOOLS`, но не в списке — модели он не виден. Так администратор
  управляет набором. Имя, которого плагин не объявил, — отказ старта.
- Остальные ключи — поля `RedisToolConfig`, вложенная таблица `[server]`
  — поля `RedisServer`.
- `[sandbox]` — изоляция для sandbox-режима: workspace, лимиты, политика
  зиготы. Сеть сюда не пишется: её объявил плагин (`network=True`), и
  сервер сам кладёт в песочницу `resolv.conf`, `hosts` и `krb5.conf`.
  Все ключи секции в разделе 8.

### Что получает тело

Вызов `redis_scan(pattern="user:*", limit=50)` хост превращает в вызов
`call redis_scan` программы плагина (в песочнице его исполняет форк
зиготы, в режиме `process` — `python -m boba.sandbox.program redis call
redis_scan`) и один JSON-объект аргументов по дескриптору `--args-fd`:

```
argv:  redis_scan --fd-result <n> --args-fd <n> ...
args:  {"pattern": "user:*", "limit": 50,
        "cfg": {"enable": true, "tools": ["redis_scan"], "max_rows": 200,
                "max_bytes": 1000000, "scan_batch": 500,
                "server": {"host": "redis.corp", "port": 6379, "db": 0},
                "sandbox": {...}}}
```

Ключ равен имени параметра тела: второй injected-параметр
`limits: Annotated[LimitsConfig, Injected]` добавил бы ключ `"limits"` с
той же секцией, а параметры контекста — ключи с их моделями. Для
`send_file(path=...)` из предыдущего раздела объект выглядит так:

```
args:  {"path": "report.csv",
        "subject": {"user_id": "f8920970-…", "login": "ivanov", "roles": ["DEV"], "profile": "general"},
        "scope":   {"kind": "chat", "id": "38586395-…"},
        "root":    {"path": "/workspace"}}
```

Тело проверяет объект моделью вызова: `pattern` и `limit` — типами и
ограничениями полей, `"cfg"` — моделью `RedisToolConfig`; отсутствующее
необязательное поле получает дефолт, лишний ключ — отказ. Потом тело зовёт
`await redis_scan(pattern=..., limit=..., cfg=...)`; `limit` больше
`max_rows` тело отказывает с числом потолка. Непригодный объект — конверт
`ReplyError` с видом `invalid_request`, и в тексте названо каждое
непригодное поле: «tool 'redis_scan': arguments do not match its
declaration: limit: …».

Так же устроен плагин `doc` в `packages/tools/boba-tool-doc`: модель
`DocToolSection` и файл `conf/plugins/doc.toml`.

---

## 3. Секрет в конфиге

У сервера появился пароль. Он обязан доехать до тела, но не попасть ни в
argv, ни в лог, ни в трейсбек. Для этого в `RedisServer` добавляется поле
типа `SecretStr`, а модель конфига наследует `SecretRevealing` вместо
`BaseModel`, как все модели конфигов плагинов репозитория:

```python
from pydantic import SecretStr

from boba.core.secrets import SecretRevealing


class RedisServer(BaseModel):
    """Адрес и пароль сервера из конфига администратора."""

    host: str = Field(min_length=1)
    port: int = Field(default=6379, ge=1)
    db: int = Field(default=0, ge=0)
    password: SecretStr = Field(min_length=1)


class RedisToolConfig(SecretRevealing, SqlLimits):
    """Сервер и лимиты выдачи; секция [tool.redis]."""

    model_config = ConfigDict(extra="ignore")

    server: RedisServer
    scan_batch: int = Field(default=500, ge=1)
```

Что делает каждая часть.

`SecretStr` маскирует значение: `repr`, `model_dump` и трейсбек
показывают `**********`. Секция плагина едет к телу значениями toml после
интерполяций, а не дампом модели, поэтому пароль доходит до тела открытой
строкой в канале аргументов и становится `SecretStr`, когда тело разбирает
секцию моделью. В argv и в журнал хоста канал аргументов не попадает.

`SecretRevealing` даёт модели метод `revealed()`: обход модели на любую
глубину, включая вложенные модели, списки и словари, который заменяет
каждый `SecretStr` открытой строкой. Хост зовёт его, когда сам кладёт в
канал аргументов модель с секретами (контекст вызова, профиль), а тесты и
стенды — когда строят секцию из готовой модели. Никаких сериализаторов
писать не нужно.

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
всех развёртываниях. Подключение описывают секцией конфига экземпляра
сервиса, а файл плагина берёт её целиком одной ссылкой `server = "${redis}"`:

```toml
[redis]
    host     = "redis.corp"
    port     = 6379
    db       = 0
    password = "..."
```

Так же сделано у плагина `kb`: `connection = "${postgres}"`. Секции, на
которые ссылаются файлы плагинов, — договор между плагином и конфигом: они
обязаны быть в каждом конфиге boba-mcp, даже если плагин выключен, иначе
сервис не стартует с именем неразрешённой ссылки. Новый плагин с новой
секцией поэтому сопровождается записью в описании версии: какую секцию
дописать в конфиги экземпляров.

Так устроен `KbToolConfig` плагина `kb`: модель наследует
`SecretRevealing`, а внутри лежит целый `PostgresConfig` с паролем или
keytab. Профиль соединения в конфиге плагина — поле-модель, наследник
`ConnectionProfile` (`ConnectionBase` наследует его): построитель
манифеста находит такие поля и называет их указателями
`connections_in_config`, например `{"pointer": "/connection", "kind":
"postgres"}`, чтобы хост заменил keytab билетом вызова (раздел 6). Профиль
внутри списка или словаря спецификация не берёт: такой конфиг не даёт
построить манифест.

---

## 4. Тип connection: свой connection у каждого пользователя

Сервер в конфиге администратора — это один сервер на всех. Обычно нужно
иначе: у пользователя есть свой redis, выданный ему лично или его роли, и
модель по имени выбирает, куда идти. Такие connection хранит чат, в своей
схеме базы (`aid` по умолчанию): таблица `connections` — id, имя, описание и
`data jsonb` с профилем, секретная часть которого зашифрована ключом
`[connections].cipher` конфига чата (ссылкой на `[keys].encryption`); таблицы `roles` и `grants`
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
        plugin.py        # PLUGIN плагина   -> boba.tools
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
redis = "boba.tool.redis.plugin:PLUGIN"
```

Имя entry point в группе `boba.connections` обязано совпадать с `kind`
профиля, реестр проверяет это на старте. Вид соединения — код сервера:
плагин называет вид строкой в манифесте, и вид, которого реестр сервера
не знает, — отказ старта. Поэтому пакет нового типа ставится в окружение
сервиса, а плагин стороннего разработчика пользуется только видами,
которые сервер уже знает.

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
которое она выбирает по выдаче `connection_list`. Манифест называет
параметр и вид connection (`"connections": {"source": "redis"}`): вид —
единственная строка `Literal` поля `kind` модели профиля. Хост ставит в
схему на это место строку с пометкой `x-boba-connection` и видом, а что на
её месте можно передать, говорят стандартные ключи схемы (подробно — ниже,
«Что предлагается вызывающему»). Для тела это готовый профиль с паролем
внутри. Ничего регистрировать не нужно: модель профиля наследует
`ConnectionProfile`, и построитель манифеста узнаёт параметр сам.

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
    limit: QueryRowLimit,
    cfg: Annotated[RedisToolConfig, Injected],
) -> MarkdownResult:
    """Скопировать ключи по шаблону с одного сервера на другой с сохранением TTL."""
    window = cfg.window(0, limit)
    src = _client(source)
    dst = _client(target)

    copied = 0
    try:
        async for key in src.scan_iter(match=pattern, count=cfg.scan_batch):
            if copied >= window.limit:
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

Объявление и файл плагина остаются обычными. Профилей соединений
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
    [sandbox.limits]
        group_memory_bytes = 1073741824
```

### Как модель узнаёт имена

Инструменты `connection_list` и `connection_search` — собственные
инструменты чата (`ConnectionTools` в `boba.connection_broker.tools`):
соединения и гранты хранит чат, поэтому они исполняются в его процессе и
читают таблицы connections/roles/grants хранилищем брокера. Агенту чат
объявляет их отдельным сервером с именем `chat`, рядом с серверами boba-mcp;
какие из них получит модель, задаёт список `tools` профиля чата
(`[profiles.<имя>].tools` конфига чата). Кто спрашивает, инструмент
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
pattern="cache:*", limit=100)`. Работа делится между чатом и сервисом boba-mcp.

Манифест плагина назвал у `redis_copy` два параметра-соединения вида
`redis`. В схеме инструмента на их месте строки с пометкой
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
5. Профили уезжают в JSON аргументов под ключами `"source"` и
   `"target"`, имена до тела не доходят.
6. Тело разбирает JSON обратно в два `RedisConnection` и работает.

В тело уезжают ровно те профили, которые назвал вызов. Остальные connection
пользователя туда не попадают, даже именами.

### Что предлагается вызывающему

Что предложить вызывающему на месте параметра-соединения, решают правила
endpoint'а в конфиге boba-mcp, `[[mcp.endpoints.<имя>.connections]]`.
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

Соединение под сервисной учёткой описывается один раз в конфиге
экземпляра boba-mcp, со своими ролями, а у инструмента — только список имён:

```toml
[connections.cache]
    kind        = "redis"
    description = "общий кэш сессий, только чтение"
    roles       = ["*"]
    [connections.cache.connection]
        host     = "redis.corp"
        port     = 6379
        password = "..."
```

`kind` — вид connection, `description` — текст для модели, `roles` — кому
соединение видно, `connection` — профиль подключения с паролем или keytab
(делегирования у таких соединений нет). В файле
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
  keytab лежит на машине приложения, которое выпускает билет, в `conf/krb`
  его экземпляра; `keytab` — имя файла там.
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
`krb5.conf` из `[env].krb` сервер кладёт в песочницу каждого плагина с
сетью (`network=True` в объявлении), биндов в файле плагина не нужно.

То же самое работает для kerberos-профиля в статическом конфиге
администратора (раздел 3), как `connection = "${postgres}"` у `kb`: хост
находит профиль в секции по указателю `connections_in_config` манифеста,
разбирает его моделью вида и подменяет kerberos-секцию билетом на каждом
вызове (`SectionTickets` в `boba.connection_broker.tickets`); тот же
билет получает подкоманда `setup` на старте. Только `kerberos_delegated`
там невозможен: делегировать некому, сессии пользователя у статического
конфига нет.

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
- Порты в манифесте — поле `stream` инструмента (`ports` с направлением,
  видами кадров и `many`, флаг барьера `gated`), а в схеме для модели на
  их месте поле канала с тем же именем: у одиночного порта — строка с
  именем канала, у порта-списка — список имён. Само значение порта строит
  тело на вызове.

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
`sandbox/plugins/<id>.ext4`, смонтированного только для чтения. Три места
описывают, что там происходит: объявление плагина говорит, нужна ли сеть и
какие образы данных (поля `network` и `data`, раздел 2), секция `[sandbox]`
файла плагина задаёт изоляцию и лимиты на каждый вызов, секция
`[tool.boba.sandbox]` в pyproject задаёт, что положить в образ при сборке.

### Что сервер выводит из манифеста

Профиль песочницы плагина сервер собирает сам из дефолтов, каталога
программы (`ProgramRoot`: `libexec`, `models`, `sandbox`), каталога данных
экземпляра (`[instance] data_dir`) и манифеста:

- `network = true` в манифесте — сеть хоста и read-only файлы
  `/etc/resolv.conf`, `/etc/hosts`, CA-файл контура `[instance] ca_file` на
  `/etc/ssl/certs/ca-certificates.crt` (с `SSL_CERT_FILE` на него) и
  `conf/krb/krb5.conf` экземпляра на `/etc/krb5.conf`, если он есть; без сети
  их нет;
- каждый образ `data` — `<программа>/models/<имя>.ext4`, смонтированный fuse2fs
  read-only в точку `target` один раз на жизнь зиготы, с нейтральным
  источником: пути хоста в `mountinfo` тела нет. Точка обязана быть в
  образе корня (`data` в `[tool.boba.sandbox]`), образ данных собирает
  `make -C build models-image`.

Подкоманда `manifest` идёт в профиле без сети, образов данных и workspace:
манифест о них ещё не сказал. `setup` и зигота — в полном профиле плагина.

### `[sandbox]` в файле плагина

Модель секции запрещает неизвестные ключи: опечатка — ошибка старта.

| Ключ | Что делает |
|---|---|
| `workspace` | `true` — монтирует ext4-образ воркспейса пользователя в `/workspace` и делает его рабочим каталогом; по умолчанию воркспейса нет, рабочий каталог `/tmp` |
| `[sandbox.limits]` | `process_memory_bytes` (1 GiB), `process_cpu_sec`, `process_file_bytes`, `process_open_files` (1024), `group_memory_bytes` — обязателен, кодом не выводится, `group_cpu_percent` (100 = одно ядро), `group_pids_max` (256), `timeout_sec` (86400) |
| `[sandbox.zygote]` | `max_start_attempts`, `restart_backoff_sec`, `start_timeout_sec`; `start_timeout_sec` — заодно срок подкоманд `manifest` и `setup` |
| `[sandbox.host]`, `[sandbox.isolation]`, `[sandbox.run]` | поля профиля поверх собранного дефолта |
| `profile` | полный профиль ссылкой `"${sandbox.profiles.<имя>}"` вместо всех ключей выше |

Так выглядит секция плагина `kb`, которому нужны сеть, kerberos, веса
модели и много памяти; сеть и веса `fastembed` объявляет его `ToolPlugin`,
в файле остаются только лимиты:

```toml
[sandbox]
    [sandbox.limits]
        process_memory_bytes = 17179869184
        group_memory_bytes   = 8589934592
        group_cpu_percent    = 400
```

Ориентиры: `bash.toml` — `workspace = true`; `doc.toml` — воркспейс, а
образ весов OCR `rapidocr` объявляет плагин `doc`.

Тело, убитое лимитом, отчитаться не успевает, и вызов без конверта
песочница объясняет по коду возврата: 152 — `process_cpu_sec`, 153 —
`process_file_bytes`, 137 и 134 — память, таймаут — `timeout_sec`.

### `[tool.boba.sandbox]` в pyproject

Секцию читает `make -C build plugin-rootfs PLUGIN=<id>`: цель находит
пакет плагина по entry point'у, собирает образ `runtime/sandbox/plugins/<id>.ext4`
и кладёт в его корень программу `/plugin/run` —
`exec python3 -m boba.sandbox.program <id> "$@"`. Python-часть образа
декларировать не нужно: в него автоматически ставится закрытие
`payload`-зависимостей пакета. Секция описывает остальное:

- `imports` — модули смоук-проверки после сборки.
- `apt` — нативные debian-пакеты: утилиты, разделяемые библиотеки, шрифты.
  Общая база образов (стадия `sandbox-base`) несёт только библиотеки
  интерпретатора python и kerberos, поэтому всё, что зовёт тело или модель
  (`curl`, `jq`, `unzip`, `pdftotext`), плагин объявляет здесь сам: так
  набор утилит bash объявляет `boba-tool-shell`.
- `data` — пути внутри образа, куда сервер подмонтирует данные: веса
  моделей, словари OCR. Сборка создаёт точку монтирования, а сами данные
  приезжают образом из `data` объявления плагина (`DataImage`), точка —
  его `target`.
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
хоста по протоколу каналов и спецификации манифеста. Два плагина одного
пакета (`confluence` и `ingest`) — два образа из одного пакета. Песочница и образы корней плагинов нужны
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
единственным параметром-моделью конфига; декоратор возвращает объект
`WarmupHook`, и объявление плагина перечисляет его в `warmups`. Так делает
`kb`:

```python
LLM: Final = LlmProviders(LlmProviderTypes.installed())


@warmup
async def warm_embedder(cfg: KbWarmupConfig) -> None:
    """Модель ONNX поднимается в зиготе: дети берут её через COW."""
    embedder = LLM.embedding(cfg.embedding)
    await embedder.embed_query("warm-up")


KB: Final = ToolPlugin(
    id="kb", tools=tuple(KB_TOOLS), warmups=(warm_embedder,), network=True
)
```

Хост первым сообщением отдаёт зиготе секцию `[tool.<id>]` значениями toml,
и каждый прогрев разбирает её своей моделью до готовности зиготы. В режиме
`process` прогрев не действует.

---

## 9. Зависимости и импорты: граница хоста и песочницы

Сервер кода плагина не импортирует, но объявление плагина импортируют
три места: подкоманда `manifest` на старте сервера (короткий запуск, по
которому меряется старт), режим `process` из окружения разработчика и
тесты. Пакет типа connection сервер импортирует сам. Отсюда правило: **на
верхнем уровне модулей, до которых дотягивается объявление плагина или
манифест типа, не должно быть ни одного импорта из `payload`.**

Дотягивается объявление далеко. Программа импортирует `plugin.py`, тот —
`tools.py`, тот — модели профилей и клиентов. Любой верхний импорт по этой
цепочке грузится в каждом `manifest`, даже если ни одна его строка там не
выполняется, а через манифест типа — и в процесс сервиса.

Что делать с тем, что нужно только телу:

```python
"""Инструменты ch: запрос идёт из песочницы."""

from typing import Any

from boba.db.clickhouse import ClickHouseError, ClickHouseQueryError
from boba.toolkit.sql import SqlErrorKind


def _payload() -> Any:
    """Клиент базы: тянет clickhouse-connect, которого в приложении нет.

    Модуль читают ради объявлений, а драйвер нужен только телу, поэтому
    импорт отложен до самого вызова.
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

Проверяется это одной командой: она импортирует объявление в отдельном
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

Программа из исходников находит свой каталог одной переменной
`BOBA_RUNTIME` — каталогом `runtime/` репозитория. Из него берётся только
собранное (`libexec`, `models`, `sandbox`; руководство администратора,
раздел 3), а всё, что есть в git, — из дерева исходников рядом: статика чата
из ассетов пакета boba-aid. Собранная Nuitka программа находит свой каталог
рядом с бинарником, и переменная ей не нужна. Различие держит один объект:
`ProgramRoots().locate()` (`boba.runtime.config`) отдаёт реализацию
`ProgramRoot` — `SourceProgram` для исходников или `InstalledProgram` для
собранной и установленной программы, — и потребители по виду не ветвятся. Службы запускаются подкомандой `serve`, проба
готовности — `health`:

```bash
BOBA_RUNTIME=$PWD/runtime .venv/bin/python -m boba.mcp_server serve \
    --config debug/mcp/conf/config.toml
```

Каталог данных экземпляра называет `[instance] data_dir`, раскладка внутри
фиксирована (`InstanceData`), `conf/krb` лежит рядом с `config.toml`
(`InstanceConf`). Компонент, которому нужен путь в данных, берёт секцию
`[instance]` и зовёт `instance.data(InstanceData.<часть>)`, а не заводит ключ
с путём.

Тело запускается без хоста той же программой плагина, что и в песочнице:
подкоманда `call <инструмент>`. Отличие от запуска launcher'ом только в
том, как передаются аргументы: вместо дескриптора `--args-fd` человек даёт
файл `--args`, а результат читает из stdout.

| Флаг | Кто передаёт | Зачем |
|---|---|---|
| `--args <файл>` | человек | JSON-объект аргументов: тот же объект, что launcher шлёт по дескриптору |
| `--args-fd <n>` | launcher | дескриптор с тем же JSON |
| `--fd-result <n>` | launcher | куда писать конверт; без него `content` печатается в stdout |
| `--fd-in <порт>=<n>` | launcher | канал входного порта; по флагу на каждый канал, у порта-списка их несколько |
| `--fd-out <порт>=<n>` | launcher | канал выходного порта, так же по флагу на канал |
| `--fd-progress <n>` | launcher | канал отчётов о ходе работы (`ToolProgress`) |
| `--fd-gate <n>`, `--fd-verdict <n>` | launcher | пара пайпов барьера группы (`StreamGroup`); без них `ready()` возвращается сразу |
| `--artifact` | человек | вдобавок к `content` напечатать JSON артефакта |

В файле — всё, что нужно телу: аргументы модели, секция плагина и
профили connection, ключом по имени параметра:

```bash
cat > /tmp/redis.json <<'EOF'
{
  "pattern": "cache:*",
  "limit": 100,
  "source": {"kind": "redis", "host": "redis.corp", "db": 0, "password": "…"},
  "target": {"kind": "redis", "host": "redis-staging.corp", "db": 0, "password": "…"},
  "cfg": {"max_rows": 100, "max_bytes": 1000000, "scan_batch": 200}
}
EOF

.venv/bin/python -m boba.sandbox.program redis call redis_copy \
    --args /tmp/redis.json --artifact
```

Подсказку по параметрам печатает
`python -m boba.sandbox.program redis call redis_copy --help`: первая
строка описания и имена параметров — ключи объекта. Манифест плагина
печатает `python -m boba.sandbox.program redis manifest` с секцией на
stdin.

Профиль с keytab так передать нельзя: тело умеет только билет, а дамп
модели с keytab падает «may not leave the application». Для отладки
kerberos-connection в файл подставляется готовая секция `kerberos_ticket`
либо профиль с паролем.

Значения контекста подаются тем же файлом: у CLI нет пользователя, и
`boba.runtime.toolcli` из toml их не соберёт, а назовёт параметр, который
нужно подать в файле `--args`. Для `send_file` файл выглядит так:

```json
{
  "path": "report.csv",
  "subject": {"user_id": "00000000-0000-0000-0000-000000000007", "login": "dev", "roles": [], "profile": "general"},
  "scope": {"kind": "chat", "id": "11111111-1111-1111-1111-111111111111"},
  "root": {"path": "/home/dev/workspace"}
}
```

Секцию плагина по toml приложения подставляет CLI хоста, и под ним же тело
можно запустить под отладчиком (цель «tool» в `launch.json`). CLI находит
плагин по id, читает конфиг boba-mcp (`--config`, повторный — наложение),
в котором есть секции плагинов, кладёт секцию
`[tool.<id>]` на места параметров роли `config`, если файл `--args` их не
дал, и исполняет `call` в своём процессе:

```bash
echo '{"pattern": "user:*", "limit": 50}' > /tmp/scan.json
.venv/bin/python -m boba.runtime.toolcli redis redis_scan \
    --config debug/mcp/conf/config.toml \
    --args /tmp/scan.json
```

Что программа каждого плагина отдаёт манифест по секции из конфига
сервиса, проверяет тест `test_plugin_manifests.py` пакета boba-stand.
Окружение разработчика готовит `source dev.sh` в корне репозитория: оно ставит `.venv`,
выполняет `uv sync --all-packages --all-extras` и называет каталог программы
`BOBA_RUNTIME=<репозиторий>/runtime`. Тесты идут из дерева
отладки и берут файлы конфигурации из переменной `BOBA_CONFIG_PATH` —
конфиг экземпляра и наложение стенда через `:`, как описано в
руководстве администратора (раздел 8, «Тесты»); долгий прогон запускают в
своей сессии tmux с выводом в файл:

```bash
source dev.sh
cd debug/aid
BOBA_CONFIG_PATH=$PWD/conf/config.toml:$PWD/conf/stand.toml \
    ../../.venv/bin/pytest ../../packages/testing/boba-stand/tests/test_plugin_manifests.py -q
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
- Старт boba-mcp падает «plugin '<id>': conf/plugins/<id>.toml is given,
  but its image … is missing» или «plugin image … has no config
  conf/plugins/<id>.toml»: комплект неполон — образ не собран
  (`make plugin-rootfs PLUGIN=<id>`), лишний образ остался в
  `sandbox/plugins` или нет файла плагина в каталоге, который конфиг сервиса
  подключает ключом `tool`.
- Старт падает «plugin <id>: `manifest` in … exited with code 3:
  [tool.<id>] does not fit <Модель>: …»: секция плагина не подходит его
  модели конфига; в тексте названы поля. Код 1 — объявление не по
  спецификации (например, injected-параметр не модель или профиль
  соединения в списке конфига), код 2 — плагина с таким id нет в
  окружении программы.
- Старт падает «… printed … bytes that are not a plugin manifest of spec 1»
  или «plugin manifest spec … is not supported»: образ собран другой
  версией SDK, `make plugin-rootfs PLUGIN=<id>`.
- Старт падает «[tool.<id>] tools names […], which plugin '<id>' does not
  declare»: в списке `tools` файла плагина имя, которого нет в манифесте.
- Старт падает «plugin <id>: `setup` … preparing its storage failed: …»:
  хранилище плагина не подготовилось — база недоступна или билет к ней не
  выпущен.
- Старт падает «tool '…': … needs connection type '…', which this server
  does not know»: манифест называет вид соединения, пакета которого нет в
  окружении сервиса.
- Старт падает «tool '…': the server adds fields […] that the plugin
  manifest already declares as model arguments»: аргумент модели назван
  так же, как поле хоста (`intent`, `scope_id`, параметр-соединение,
  канал); переименуйте аргумент.
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
- Тело получает `**********` вместо секрета: секцию или профиль собрали
  дампом модели без раскрытия (тест, стенд), либо у поля свой
  `field_serializer`, который маскирует.
- Вызов отвергнут «tool '…': arguments do not match its declaration: …»:
  аргументы не прошли модель вызова тела; в тексте названо каждое
  непригодное поле.
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
- Зигота плагина не поднимается: образ плагина не собран или собран до
  правок деклараций, `make plugin-rootfs PLUGIN=<id>`.
- Зигота не поднимается с текстом «zygote pid … serves a plugin with
  manifest …, the server read manifest … at start: the plugin image was
  replaced»: образ
  подменили, пока сервер работал; сервер читает манифест только на старте,
  перезапустите его.
- «control closed on call» или смерть зиготы на первом вызове: гость в
  rootfs отстал от хоста, `make plugin-rootfs-all`.
- Библиотека тела грузится в `manifest` или в процесс сервиса: она
  объявлена обычной зависимостью вместо `payload`, либо импортирована на
  верхнем уровне модуля, до которого дотягивается объявление плагина или
  манифест типа (раздел 9).
- Тело не видит сеть: в объявлении плагина нет `network=True`.
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
- **Ретранслятор чата — прозрачный посредник MCP-over-ACP.** Серверу
  профиля запрос агента уходит с clientInfo и clientCapabilities агента
  (сверху — расширения, которые исполняет сам чат: журнал, запечатывание
  соединений, MCP Apps); агенту `server/discover` отдаёт возможности,
  расширения, инструкции и serverInfo сервера. Устаревшие в 2026-07-28
  логи (`notifications/message`) ретранслятор не несёт: уровень логов из
  `_meta` запроса серверу не уходит, возможность `logging` агенту не
  объявляется. Сервер `chat` объявляет только инструменты без смены
  списка; ресурсы, промпты, подсказки и потоки подписки у него — `-32601`.
- **Серверы профиля — любой редакции, агенту — всегда 2026-07-28.** Версию
  с сервером профиля согласует клиент чата штатной политикой SDK
  (`server/discover`, при отказе прежней редакции — `initialize`), и
  различие эпох агент не видит: `server/discover` ретранслятора строится из
  полей сессии, общих для обеих эпох, ответы старого сервера SDK приводит к
  моделям 2026-07-28. Различия, которых требует протокол: серверу
  рукопожатия клиент не объявляет sampling и roots, а elicitation — так,
  как объявил агент; встречный вопрос `elicitation/create` посреди вызова
  ретранслятор отдаёт агенту `InputRequiredResult` со своим `requestState`
  (`boba-aid/held/…`), держит вызов к серверу открытым (`HeldCall`) и
  отдаёт ему ответ повтора агента — агент спрашивает пользователя тем же
  путём MRTR, что и у сервера 2026-07-28. Открытый вызов живёт не дольше
  `call_timeout_sec` сервера; отменённый и просроченный убирается, сервер
  получает `cancel`. Вопрос клиент отдаёт вызову, открытому на сессии с
  приёмником вопросов (`McpToolPort.call(..., asked)`), а при двух таких
  вызовах — отказ серверу. В описании такого сервера
  нет смены списков и подписки на ресурсы (потоков `subscriptions/listen`
  у него нет), инструмент с `execution.taskSupport: "required"` агенту не
  показывается (задач клиент не объявляет). Ключи конверта версии и
  личности из `_meta` запроса агента серверу не пересылаются — их ставит
  сессия по своему проводу. На `initialize` самого агента ретранслятор
  отвечает `-32022` с перечнем своих версий. Сервер, который не открылся у
  агента, в сессию не входит: агент шлёт клиенту пометку `notice`
  (`warning`) с причиной, и ход идёт с инструментами остальных серверов.
- **Ресурсы и промпты серверов агент отдаёт модели и пользователю
  сам.** Ресурсы модель читает собственными инструментами агента
  `list_mcp_resources` (ресурсы и шаблоны серверов) и `read_mcp_resource`
  (сервер и uri). Промпты — команды сессии под именем
  `mcp__<сервер>__<промпт>`; вопрос `/mcp__<сервер>__<промпт> <аргументы>`
  агент раскрывает `prompts/get` у сервера-владельца в служебный контекст
  вопроса, слова — аргументы по порядку, лишние — последнему. Смену списков
  инструментов и промптов агент слушает потоком `subscriptions/listen` и
  объявляет модели в следующем ходе.
- **Команды сессии живут только в истории агента.** При каждом подключении,
  переподключении и обрыве сервера MCP, перед каждым запросом хода и по
  смене списков агент сверяет команды серверов (промпты и помеченные
  `com.boba/commands` интерфейсы MCP Apps), свои команды и объявление
  клиента с историей и пишет разницу служебной записью `commands-delta`
  (снятые имена и добавленные команды целиком: вид `prompt`, `app`, `agent`
  или `harness`, владелец, аргументы с описанием, обязательностью и
  значениями `completion/complete`, у интерфейса — карточка инструмента и
  `resourceUri`). Модели записи не уходят. Клиенту команды уходят из
  истории: стандартный `available_commands_update` несёт то, что исполняет
  агент, а клиенту, объявившему `_boba/commands: true` в
  `capabilities._meta` initialize, все команды целиком уведомлением
  `session/update` вида `_boba/commands`. Свои команды чат пишет в историю
  запросом `_boba/session/record` вида `commands`; меню и маршрут команд
  чата строятся только из уведомления `_boba/commands` (тест
  `TestOnlyFromHistory`). Сервер, переименованный посреди беседы, виден
  только новым именем — записями истории, без особой логики.
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
ресурсами и уведомлением об открытом журнале, ниже), `com.boba/files` (файлы workspace) и
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
  не предлагает. Первое уведомление `tool_call` этого вызова несёт в `_meta`
  признак `boba/question: true`: по нему, а не по имени инструмента, чат
  отличает вопрос пользователю от вызова инструмента — одинаково вживую и
  при воспроизведении истории, где формы уже нет. Ввод вызова (`rawInput`:
  `questions[]` с `question`, `header`, `options[]{label, description,
  preview}`, `multiSelect`) и structuredContent итога (`picks` по тексту
  вопроса — `labels[]` и `other`, `outcome` accept|decline|cancel) чат
  читает своими моделями (`boba.aid.rendering.question`), моделей агента не
  импортируя. В ленте вызов-вопрос — отдельное сообщение
  основной ленты, а не шаг контейнера хода: пока вопрос ждёт ответа —
  строка «Question» с мигающей точкой, после итога — запись ответа той же
  панелью только для чтения (выбранное отмечено, остальное приглушено, свой
  текст — под «Other»), с зелёной точкой у ответа и красной у отказа.
  Подписи панели и записи — ключи `aid.question` переводов chainlit
  (`assets/.chainlit/translations`, en-US и ru-RU); пользовательские
  элементы получают переводчик импортом `@/components/i18n` (карта импортов
  overlay `Imports.ts`), тексты вопросов и вариантов показываются как пришли.
- **input_required сервера MCP выполняет агент.** Просьбу сервера о вводе
  (`InputRequiredResult` с формами `elicitation/create`) агент задаёт
  клиенту той же формой ACP с именем сервера и инструмента в `_meta`
  (`boba/server`, `boba/tool`) и повторяет вызов с `inputResponses` и тем
  же `requestState`, но не больше `mcp.input_rounds` раз за вызов. Формы
  серверу агент объявляет в `_meta.clientCapabilities` вызова, только если
  их объявил его клиент; выборку (sampling), корни (roots) и форму-ссылку
  агент не выполняет — вызов кончается ошибкой с причиной. Раунды вопросов
  ложатся в `_meta` результата `tools/call` под ключом `boba/elicitations`
  (модель `ElicitationRound`): по ним лента рисует запись вопроса
  сообщением основной ленты после итога вызова одинаково вживую и в истории.

Журнал вызова идёт стандартными методами MCP и одним уведомлением
расширения `com.boba/journal`. Каналы журнала — ресурсы по шаблону
`boba-journal://{run}/{node}/{channel}{?offset,before}`: `resources/read`
отдаёт окно канала от `offset` или перед `before`, координаты окна лежат в
`_meta` содержимого под ключом `com.boba/journal`; канала нет или запуск
чужой — ошибка `-32602`. Итог вызова несёт ссылки `resource_link` на
каналы своего журнала.

Пока вызов идёт, о каждом открытом им журнале сервер сообщает уведомлением
`notifications/com.boba/journal/opened` в потоке ответа самого `tools/call`
(SSE ответа по streamable HTTP, тот же канал stdio с привязкой к запросу) —
до итогового ответа, как и `notifications/progress`. Параметры — модель
`JournalNotice` (`boba.toolkit.wire`):

| Поле | Смысл |
|---|---|
| `progressToken` | токен прогресса запроса, в потоке которого идёт уведомление |
| `run` | запуск — первая часть адреса журнала |
| `node` | идентификатор вызова на сервере — вторая часть адреса |
| `channels` | каналы журнала для человека (`tool_stdout`, `tool_stderr`) |
| `key` | ключ узла от автора связки, если журнал принадлежит узлу `workflow`; у вызова одного инструмента его нет |

Уведомление уходит, только если клиент объявил расширение
`com.boba/journal` в своих возможностях (`_meta.clientCapabilities`
запроса или `initialize`) и назвал токен прогресса: так клиент отдаёт его
обработчику хода своего вызова, без второго соединения и без потока
`subscriptions/listen`. Ретранслятор чата так и узнаёт журнал идущего
вызова агента и называет его ленте сообщением шины `JournalOpened`; панель
читает окна `resources/read` и слушает рост канала
(`notifications/resources/updated`) потоком `subscriptions/listen`.

Поведение без расширения (fallback по правилу Extension Negotiation):
клиенту, который `com.boba/journal` не объявил, уведомление не идёт —
он работает путём ядра. Журналы идущих вызовов вызывающего — ресурсы
`resources/list` (для человека, `audience: ["user"]`), об их открытии и
конце сервер сообщает `notifications/resources/list_changed` потоком
`subscriptions/listen`; после вызова журнал называют ссылки
`resource_link` его итога. В `server/discover` boba-mcp объявляет только
то, что даёт: инструменты без смены списка (список зависит лишь от ролей
токена), ресурсы журналов с подпиской и сменой списка; промптов, логов и
подсказок у него нет. Клиенту прежней редакции (`initialize`, без сессии) —
инструменты и ресурсы журналов без подписки и смены списка и без
расширений: его провод их не несёт.

---

## 13. Спецификация плагина, версия 1

Этот раздел — контракт между сервисом boba-mcp и плагином. Плагин,
собранный по нему кем угодно, сервер принимает без правок своего кода;
всё, что сервер знает о плагине, он узнаёт из файлов комплекта и из
вывода программы плагина. Модели манифеста живут в
`boba.toolkit.manifest`, программа — в `boba.sandbox.program`.

### Комплект

Плагин поставляется двумя файлами с одним именем — id плагина:

- `<id>.ext4` — образ корня песочницы; сервер ищет его в
  `<программа>/sandbox/plugins/<id>.ext4` (из исходников —
  `$BOBA_RUNTIME/sandbox/plugins/<id>.ext4`);
- `<id>.toml` — конфиг плагина; конфиг экземпляра подключает каталог
  `conf/plugins` ключом `tool = "${toml_dir:plugins}"`, и файл становится
  таблицей `[tool.<id>]`.

Ключа с путём к образу нет: путь выводится из id. id — единственное имя
плагина: им же названы секция конфига и entry point объявления. Конфиг без
образа и образ без конфига — отказ старта, в тексте названы оба пути.
Чтобы заменить плагин, достаточно положить два файла и перезапустить
сервис.

### Образ и программа

В корне образа обязан лежать исполняемый файл `/plugin/run` — любая
программа, которая выполняет четыре подкоманды ниже. Сборка репозитория
кладёт туда скрипт `exec python3 -m boba.sandbox.program <id> "$@"`:
программа SDK находит объявление `ToolPlugin` по entry point'у `<id>`
группы `boba.tools` в окружении образа и строит из него всё остальное.

Внутренний шаг сборки `plugin-bin` собирает тот же плагин без python:
сценарий `build/scripts/nuitka.sh` компилирует модуль `boba.sandbox.program`
в каталог `/plugin/run.dist`, а `/plugin/run` — скрипт `exec
/plugin/run.dist/run.bin <id> "$@"`. Данные и ленивые импорты чужих
пакетов, которых Nuitka не видит сама (yaml rapidocr), называет
`build/conf/nuitka.yml`. Корень образа — голый glibc с CA
контура, `apt`, `root`, `setup` и точками `data` из деклараций. Опись
сборки (`boba.core.package_data`) кладёт в бинарник файлы данных пакетов
boba, метаданные их дистрибутивов и модули entry points групп `boba.*`,
иначе поиск провайдеров внутри бинарника пуст. Перед записью образа сборка
проверяет программу в голом корне: каждая ELF находит свои библиотеки, а
снаружи `/plugin` берёт только glibc и пакеты ключа `apt`; затем идёт
подкоманда `self-check` — файлы и entry points описи на месте. Образ
ложится в `release/<версия>/plugins/<id>/<id>.ext4`; тесты песочницы
по-прежнему идут на образах с python из `runtime/sandbox/plugins`.

| Подкоманда | Вход | Выход | Кто и где зовёт |
|---|---|---|---|
| `manifest` | stdin — JSON секции `[tool.<id>]` после интерполяций | stdout — манифест JSON; stderr — причины отказа | сервер на старте, в песочнице плагина без сети, образов данных и workspace |
| `setup` | stdin — та же секция, профили соединений в ней уже с билетами вызова вместо keytab | код возврата; stderr — причина отказа | сервер на старте, если манифест объявил `setup`; полный профиль плагина, с сетью |
| `zygote` | аргументы `--socket-fd`, `--reap-poll-sec`, `--log-level`; первое сообщение по сокету — секция для прогревов | сообщение готовности с отпечатком манифеста, затем вызовы форком | сервер, на жизнь сервиса; полный профиль плагина |
| `call <инструмент>` | JSON-объект аргументов по `--args-fd` (человек — файлом `--args`), каналы флагами `--fd-*` | конверт `ReplyOk`/`ReplyError` в `--fd-result` (без него — `content` в stdout) | зигота на каждый вызов; режим `process` и человек — напрямую |

Коды возврата `manifest` и `setup` (`ProgramExit`): `0` — готово, `1` —
подкоманда сорвалась (хранилище не подготовилось, объявление не
укладывается в спецификацию), `2` — командная строка не разобрана (нет
подкоманды, плагина с таким id нет в окружении), `3` — секция не подходит
моделям конфига плагина. Ненулевой код — отказ старта сервера с текстом
stderr программы. Подкоманда `call` отвечает кодами `ToolMain.Exit`: `0`,
`1` — тело подняло ошибку, `2` — нарушен контракт запуска (неизвестный
инструмент, непригодные аргументы); исход вызова сервер берёт из
конверта, а не из кода.

`manifest` проверяет секцию всеми моделями конфига плагина: моделями
параметров роли `config`, прогревов и хранилища. Сервер ключей плагина не
знает, поэтому ошибку в секции называет сам плагин: «[tool.kb] does not fit
KbToolConfig: connection: Field required [missing]».

### Манифест

Манифест — JSON-объект модели `PluginManifest`, лишние поля запрещены:

| Поле | Тип | Смысл |
|---|---|---|
| `spec` | целое | версия спецификации; сервер знает ровно одну, `PluginManifest.SPEC = 1` |
| `id` | строка | id плагина; обязан совпасть с именем комплекта |
| `version` | строка | версия плагина (у программы SDK — версия дистрибутива) |
| `setup` | bool | у плагина есть хранилище, сервер зовёт подкоманду `setup` |
| `warmups` | список `{module, hook}` | прогревы зиготы |
| `data` | список `{name, target}` | образы данных: `<программа>/models/<name>.ext4`, смонтированный read-only в `target` |
| `network` | bool | телу нужна сеть: сеть хоста, `resolv.conf`, `hosts`, `krb5.conf` |
| `connections_in_config` | список `{pointer, kind}` | профили соединений внутри секции: JSON-указатель от корня секции (`/connection`) и вид |
| `tools` | список `ToolDecl` | инструменты плагина |

Инструмент (`ToolDecl`):

| Поле | Смысл |
|---|---|
| `name`, `description` | имя и описание для модели |
| `hints` | `{read_only, destructive}` — пометки MCP для решения о подтверждении |
| `input_schema` | JSON Schema аргументов модели: `type`, `properties`, `required`, `$defs`; у аргумента-кода `contentMediaType`, у аргумента, который сервер не пишет в лог, — `x-boba-not-logged: true`. Служебных параметров в ней нет |
| `injected` | служебные параметры тела: имя → роль |
| `connections` | параметры-соединения: имя → вид соединения (`postgres`, `clickhouse`, `oracle`, `web`, …) |
| `stream` | потоковая декларация: `ports` (`name`, `direction`, `kinds`, `raw`, `many`) и `gated` — барьер группы |
| `results` | виды результата из аннотации возврата тела |

Роли служебных параметров (`InjectedRole`): `config` — секция
`[tool.<id>]` целиком, `subject`, `scope`, `workspace_root` — контекст
вызова (пользователь, область, корень workspace), `progress` — отчёты о
ходе работы, их строит само тело.

Пример — манифест плагина `mail`, описание сокращено:

```json
{
  "spec": 1, "id": "mail", "version": "0.0.30.dev4",
  "setup": false, "warmups": [], "data": [], "network": true,
  "connections_in_config": [],
  "tools": [{
    "name": "mail",
    "description": "Выполнить отправку почты email.",
    "hints": {"read_only": false, "destructive": true},
    "input_schema": {
      "type": "object",
      "properties": {"address": {"type": "string", "minLength": 1, "title": "Address",
                                 "description": "emal адрес на который отправить сообщение"}},
      "required": ["address"]
    },
    "injected": {"cfg": "config"},
    "connections": {},
    "stream": {"ports": [], "gated": false},
    "results": ["markdown"]
  }]
}
```

Манифест пишет не автор: программа SDK строит его построителем
`ManifestBuilder` из объявления `ToolPlugin` и аннотаций тел. Профиль
соединения в параметре и в конфиге построитель узнаёт по базе
`ConnectionProfile`, вид — по `Literal` поля `kind`; модель контекста — по
базе `ContextValue` и её `ROLE`; любая другая модель с маркером `Injected`
— роль `config`. Профиль внутри списка или словаря конфига, параметр
`Injected` не моделью и профиль без единственного вида — ошибка построения
(`ManifestError`), `manifest` отвечает кодом 1.

### Что проверяет сервер и что — плагин

Сервер разбирает в секции плагина только ключи фреймворка моделью
`PluginMeta`: `enable`, `tools` (имя, которого нет в манифесте, — отказ
старта), `connections` (заданные соединения сервера на инструмент) и
таблицу `[sandbox]` (изоляция и лимиты, раздел 8). Остальные ключи секции
проверяет плагин подкомандой `manifest`.

Аргументы модели сервер не проверяет. Он ставит в схему для клиента свои
поля — подпись вызова `intent`, область `scope_id`, имена
параметров-соединений, поля каналов и `pipe_bytes` — рядом с
`input_schema` манифеста; совпадение имени поля сервера с аргументом
модели — отказ старта. На вызове сервер собирает один JSON-объект:
аргументы модели как прислала модель, секцию плагина на местах роли
`config` (keytab-профили по `connections_in_config` заменены билетом
вызова), значения контекста и профили соединений; объект уезжает телу по
`--args-fd`. Тело проверяет объект моделью вызова, и непригодный аргумент
возвращается обычным результатом-отказом с каждым полем в тексте. Канал
аргументов не ограничен размером argv.

### Что остаётся за сервером

Виды результата (`boba.toolkit.result`) — код сервера и клиента: плагин
возвращает только их, своих видов в версии 1 нет. Виды соединений —
тоже код сервера (пакеты `boba-db-*`, `boba-transport-http`, группа entry
points `boba.connections`): плагин называет вид строкой, и вид, которого
сервер не знает, — отказ старта. Контекст вызова, журналы, права по имени
инструмента, каналы и насосы сервер ведёт по данным манифеста.

### Версионирование

Номер `spec` покрывает манифест и провод «сервер ↔ зигота и вызов»:
сообщения зиготы, запрос вызова, JSON аргументов, флаги каналов и конверт
ответа. Несовместимая правка любого из них поднимает номер. Сервер
поддерживает одну версию, без слоёв совместимости: манифест другой версии
отвергается текстом «plugin manifest spec 2 is not supported: this server
reads spec 1 only», и плагин пересобирается под сервер.

### Порядок старта сервиса

1. Найти комплекты: id — таблицы `[tool.*]` конфига; в режиме песочницы у
   каждого конфига есть образ и у каждого образа конфиг (`check_kits`).
2. Запустить `manifest` у всех включённых плагинов одновременно. Это
   короткий запуск: 0,6–2,1 с на плагин по отдельности, 2,35 с на все 13
   плагинов репозитория вместе.
3. Запустить `setup` у тех, чей манифест его объявил, с секцией, в которой
   keytab-профили заменены билетами.
4. Собрать реестр инструментов по манифестам.
5. Поднять зиготы. Зигота при готовности называет отпечаток манифеста
   (sha256 его JSON); отпечаток, не совпавший с прочитанным на шаге 2,
   значит, что образ подменили, пока сервис работал: такая зигота не
   принимается, и плагин гаснет с ошибкой «the plugin image was replaced,
   restart the server to read it again». Новый образ сервер читает только
   при следующем старте.

В режиме `process` шаги те же, программа плагина —
`python -m boba.sandbox.program <id>` интерпретатором сервиса из окружения
разработчика, без песочницы и зигот.
