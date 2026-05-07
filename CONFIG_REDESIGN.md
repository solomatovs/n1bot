# Конфигурирование: целевая архитектура

Статус: дизайн до реализации. Документ описывает **целевую** модель;
ссылки на текущий код встречаются только в §10 (миграция).

## 1. Цели

* Источник конфига — плоское пространство `FlatConfig`, неизменное в этом
  дизайне.
* **Концепт «регистрация секции» удаляется как явление.** Вся инфра
  pre-build регистрации (`ConfigSection`/`AppConfigBootstrap.register_section`/
  `discover_extension_sections`/entry-point group `boba.config_sections`/
  `ConfigSectionFactory`/`AppConfig.section(...)`) выкидывается. DTO любого
  компонента материализуется лениво вызовом `bundle.materialize(schema, prefix)` —
  schema приносит caller (плагин в `config()`, composition root для core-DTO),
  без предварительной декларации фреймворку. Двусторонний контракт
  «зарегистрируй, потом запрашивай» заменяется на односторонний «опиши
  schema там, где она нужна, и материализуй на месте».
* Плагин не знает свой mount path; путь определяет app по convention
  `tool.<name>`.
* Дублирование значений в источниках устраняется ссылками `@{path}` внутри
  FlatConfig (eager-resolve, полная замена).
* Подключение плагина управляется флагом `enable` рядом с его конфигом.
* Tools из плагинов и других источников (MCP, удалённые провайдеры)
  объединяются в едином `ToolsService` через `ToolSource`.
* **Tool остаётся чистым**: принимает готовый config-DTO и `ExtensionContext`
  в `__init__`, реализует `definition()`/`execute()` как сейчас. Никаких
  ClassVar `NAME`/`CONFIG_SCHEMA`/`CANONICAL` framework на нём не требует —
  Tool ничего не знает о плагин-инфре.
* **Plugin** — отдельная сущность, владеющая всем, что относится к
  конфигурированию и сборке: `NAME`, `config()` (возвращает плоскую
  `ObjectSchema` — общие connection-поля + per-tool `PromptOverlay`),
  `build(cfg, ctx)` (создаёт Tool-DTO inline из общих полей и
  соответствующего overlay'я, инстанцирует Tools, упаковывает в
  `ToolSource`). Plugin всегда возвращает один `ToolSource` с массивом
  tools (даже если tool один) — единый контракт.
* Описания tool'а для LLM живут в одном месте — внутри Tool. Конкретно:
  Tool строит `ObjectSchema` прямо в `definition()` и сразу мержит её
  с overlay'ем: `return self._cfg.prompt.apply(ObjectSchema(...))`.
  Никаких ClassVar `CANONICAL` или скрытых атрибутов — schema живёт
  ровно одним выражением там, где она нужна. Это convention для
  Tool-разработчиков, не часть framework.

## 2. Состав

### 2.1. `boba.config.flat.FlatConfig`

```python
@dataclass(frozen=True)
class FlatConfig(ConfigSpace):
    values:  Mapping[ConfigPath, ConfigValue]
    origins: Mapping[ConfigPath, OriginChain]
```

Поведение `lookup`, `keys_under`, `child_segments`, `subtree` — без изменений.
К моменту, когда внешний код видит `FlatConfig`, ссылки уже разрешены.

### 2.2. `boba.config.refs`

```python
@dataclass(frozen=True)
class OriginStep:
    path: ConfigPath
    source: str

OriginChain = tuple[OriginStep, ...]

class ReferenceResolver:
    def resolve(
        self,
        values: Mapping[ConfigPath, ConfigValue],
        origins: Mapping[ConfigPath, str],
    ) -> tuple[Mapping[ConfigPath, ConfigValue], Mapping[ConfigPath, OriginChain]]: ...

class UnresolvedRefError(Exception): ...
class CircularRefError(Exception): ...
class RefDepthExceededError(Exception): ...
```

Подробности — в §3.

### 2.3. `boba.declaration.NestedField` — расширение

В существующих типах `declaration.py` пара «scalar / object» симметрична
только на уровне `ItemReader`:

| Уровень        | Скаляр       | Объект        |
|----------------|--------------|---------------|
| `FieldKind`    | `FieldSpec`  | **отсутствует** |
| `ItemReader`   | `ScalarItem` | `ObjectItem`  |

Для Plugin-схемы (§2.6) нужен одиночный nested-объект как поле schema —
добавляем недостающую клетку:

```python
@dataclass(frozen=True)
class NestedField(FieldKind, Generic[V]):
    """Одиночное nested-поле: рекурсивная схема под sub-prefix'ом."""
    name: str
    schema: ObjectSchema[V]
    description: str = ""
```

Плюс одна ветка в `FlatConfigMaterializer._read_field`:

```python
case NestedField(name=name, schema=nested):
    return FlatConfigMaterializer(nested).materialize(
        space, prefix.join(NameSegment(name)),
    )
```

Тело идентично существующей ветке для `ObjectItem` в `_read_item`. Всё
расширение — ~10 строк.

### 2.4. `boba.config.bundle.ConfigBundle`

```python
@dataclass(frozen=True)
class ConfigBundle:
    flat: FlatConfig

    @classmethod
    def from_sources(cls, sources: Iterable[ConfigSource]) -> ConfigBundle: ...

    def materialize(self, schema: ObjectSchema[T], prefix: ConfigPath) -> T: ...
```

Внутри `from_sources`: fold источников по priority → `_MergeState` →
`ReferenceResolver.resolve(...)` → `FlatConfig`. Внешний API не меняется.

### 2.5. `Tool`-контракт (без изменений)

Tool — domain-класс из `boba.tools.domain.tool.Tool`. Дизайн **не
расширяет** его — Tool остаётся ровно тем, чем является сейчас:

```python
class Tool(
    Executor[ToolContext, TArgs, ToolResult],
    Definition[ObjectSchema[TArgs]],
    Generic[TArgs],
):
    @abstractmethod
    def tool_id(self) -> ToolId: ...
    @abstractmethod
    def tool_source_id(self) -> ToolSourceId: ...
    @abstractmethod
    def definition(self) -> ObjectSchema[TArgs]: ...
    @abstractmethod
    def execute(self, ctx: ToolContext, args: TArgs) -> ToolResult: ...
    def args_converter(self) -> Converter[dict[str, Any], TArgs]: ...   # default
```

`__init__(cfg, ctx)` — convention: Tool принимает готовый config-DTO и
`ExtensionContext`. Никаких ClassVar (NAME, CONFIG_SCHEMA, CANONICAL)
framework не требует — Tool ничего не знает о плагин-инфре.

Schema args для LLM строится прямо в `definition()` с одновременным
применением `prompt`-overlay (см. §6.1): `return cfg.prompt.apply(ObjectSchema(...))`.
Никаких отдельных хранилищ canonical schema на классе.

### 2.6. `boba.plugin` — Plugin-контракт

Размещается в `packages/boba-core/src/boba/plugin/__init__.py`.
Plugin владеет всем, что относится к конфигурированию и сборке Tool'ов.

```python
class ExtensionContext:
    """Канал общих сервисов (logger, метрики и т. п.). Точное содержимое — см. §9.2."""

class Plugin(Protocol):
    NAME: ClassVar[StrId]                              # имя плагина (mount path = tool.<NAME>)

    @classmethod
    def config(cls) -> ObjectSchema[Any]: ...          # плоская schema: общие поля + per-tool prompts

    @classmethod
    def build(cls, cfg: Any, ctx: ExtensionContext) -> ToolSource: ...
```

**Единый контракт без mixin'ов и шаблонов**:

* Plugin **всегда** возвращает один `ToolSource` с массивом tools.
  Размер массива (1 или N) — деталь плагина.
* `config()` собирает плоскую `ObjectSchema` плагина: общие
  connection-поля на корне + по одному `prompt_field(tool_name)` на
  каждый tool. Никаких вложенных Tool-DTO в Plugin DTO.
* `build` собирает Tool-DTO inline из общих полей и соответствующего
  overlay'я, инстанцирует Tools, упаковывает в `StaticToolSource` (или
  специализированный source — для MCP).
* Tool-схемы для materializer'а как отдельные сущности **не нужны** —
  Tool обходится своим dataclass-DTO для типизации `__init__`.

### 2.7. `boba.plugin.prompt` — overlay для описаний

Размещается в `packages/boba-core/src/boba/plugin/prompt.py`.
Convention: Tool кладёт в свой config-DTO поле `prompt: PromptOverlay`
и в `definition()` строит свою `ObjectSchema` локально, оборачивая её в
`self._cfg.prompt.apply(...)`. Framework предоставляет
`PromptOverlay`, готовую `PROMPT_OVERLAY_SCHEMA` и хелпер `prompt_field()`
для использования в Plugin.config().

```python
@dataclass(frozen=True)
class PromptOverlay:
    description: str | None = None
    fields: Mapping[str, str] = field(default_factory=dict)

    def apply(self, schema: ObjectSchema[T]) -> ObjectSchema[T]:
        """Применить overlay к tool-схеме: вернуть копию с подставленными описаниями."""
        new_description = self.description if self.description else schema.description
        new_fields = []
        for f in schema.fields:
            if not isinstance(f, FieldSpec):
                new_fields.append(f)
                continue
            new_desc = self.fields.get(f.name, f.description)
            new_fields.append(replace(f, description=new_desc))
        return replace(schema, description=new_description, fields=tuple(new_fields))


PROMPT_OVERLAY_SCHEMA: ObjectSchema[PromptOverlay] = ObjectSchema(
    description="Overlay описаний tool'а: общее description и per-field overrides.",
    fields=[
        FieldSpec(
            name="description",
            coercer=ChainCoercer(Default(None), ParseString()),
        ),
        CollectionField(
            name="fields",
            reader=ScalarItem(coercer=ChainCoercer(Default(""), ParseString())),
            shape=KeyedShape(),
        ),
    ],
    factory=PromptOverlay,
)


def prompt_field(name: str) -> NestedField[PromptOverlay]:
    """Convenience: NestedField для PromptOverlay под указанным именем."""
    return NestedField(name=name, schema=PROMPT_OVERLAY_SCHEMA)
```

`ObjectSchema` и `FieldSpec` — `frozen=True`, поэтому `apply` возвращает
новую копию.

### 2.8. `boba.plugin.discovery`

Размещается в `packages/boba-core/src/boba/plugin/discovery.py`.

```python
def discover_plugins(group: str = "boba.plugins") -> Iterable[type[Plugin]]: ...
```

Возвращает **классы**. Один пакет может декларировать несколько плагинов.

### 2.9. `RunningApp`

```python
@dataclass(frozen=True)
class RunningApp:
    core: AppCoreConfig
    agent: AgentConfig
    workspaces: WorkspacesConfig
    tools_service: ToolsService    # tools из всех источников: плагинов, MCP, ...
    ...
```

`ToolsService`, `ToolFactory`, `ToolCatalog`, `ToolSource`,
`StaticToolSource`, `ToolSourceId` — существующие абстракции
(`boba.tools.framework.registry`), сохраняются. Меняется ровно одна
деталь: `ToolsService.__init__` принимает готовый `ToolCatalog`, а
мутирующий `rebuild_catalog()` удаляется. Добавляется classmethod
`ToolsService.from_sources(sources)` для composition root.

```python
class ToolsService(Executor[ToolContext, ToolCall, ToolResult]):
    def __init__(self, catalog: ToolCatalog) -> None:
        self._catalog = catalog

    @classmethod
    def from_sources(cls, sources: Iterable[ToolSource]) -> ToolsService:
        factory = ToolFactory()
        for source in sources:
            factory.register(source)
        return cls(factory.build())

    # tools(), definitions(), get(), execute(), _unknown_tool() — без изменений
```

## 3. Reference resolver

### 3.1. Синтаксис

Ссылка — строковое значение, **полностью** соответствующее паттерну
`@{path}`, где `path` — абсолютный путь в нотации `a.b.c[0].d`. Только
полная замена.

### 3.2. Фаза разрешения

Eager. Между fold источников и созданием `FlatConfig`:

```
sources fold → _MergeState (с ссылками)
            → ReferenceResolver.resolve()
            → FlatConfig (без ссылок)
```

### 3.3. Алгоритм

DFS по leaf-значениям, `visited`-set по пути для обнаружения циклов,
лимит глубины. Ошибки: `UnresolvedRefError`, `CircularRefError`,
`RefDepthExceededError`.

### 3.4. Origin chain

Для пути без ссылок `OriginChain` имеет длину 1. Для ссылочной цепочки —
последовательность шагов от ref-узла к финалу с указанием источника
каждого узла.

## 4. Plugin enable-convention

Плагин подключается только если в его секции конфига явно `enable = true`.

```toml
[tool.confluence]
enable = true
```

* `enable` **не объявляется в Plugin-схеме** — это convention app.
* Default — false.
* Если плагин выключен, его DTO **даже не материализуется**.

```python
def _is_enabled(bundle: ConfigBundle, mount: ConfigPath) -> bool:
    lookup = bundle.flat.lookup(mount.join(NameSegment("enable")))
    if not lookup.is_found():
        return False
    return _coerce_bool(lookup.value())
```

## 5. Mount-convention

```python
def _mount_path_for(plugin_name: StrId) -> ConfigPath:
    return ConfigPath.parse(f"tool.{plugin_name}")
```

## 6. Plugin: единый контракт на одной картине

### 6.1. Tool — чистый domain-класс

Tool принимает готовый config-DTO и `ExtensionContext`. `definition()`
строит `ObjectSchema` локально и сразу заворачивает её в
`prompt.apply(...)` — никаких ClassVar или хранилищ canonical schema.

```python
class ConfluenceSearchTool(Tool[SearchArgs]):
    def __init__(self, cfg: ConfluenceSearchToolConfig, ctx: ExtensionContext) -> None:
        self._cfg = cfg
        self._ctx = ctx

    def tool_id(self) -> ToolId:
        return ToolId("confluence_search")

    def tool_source_id(self) -> ToolSourceId:
        return ToolSourceId("plugin.confluence")

    def definition(self) -> ObjectSchema[SearchArgs]:
        return self._cfg.prompt.apply(ObjectSchema(
            description="Полнотекстовый поиск страниц Confluence.",
            fields=[
                FieldSpec(name="query", description="Поисковый запрос (обычный текст).",
                          coercer=ChainCoercer(NonEmpty(), IsString()), required=True),
                FieldSpec(name="limit", description="Максимум hits в ответе.",
                          coercer=ChainCoercer(IsInt(), MinValue(1), MaxValue(50)),
                          required=True),
            ],
            factory=SearchArgs,
        ))

    def execute(self, ctx: ToolContext, args: SearchArgs) -> ToolResult: ...


class ConfluencePageTool(Tool[PageArgs]):
    def __init__(self, cfg: ConfluencePageToolConfig, ctx: ExtensionContext) -> None: ...
    def tool_id(self): return ToolId("confluence_page")
    def tool_source_id(self): return ToolSourceId("plugin.confluence")
    def definition(self): return self._cfg.prompt.apply(ObjectSchema[PageArgs](...))
    def execute(self, ctx, args): ...


class ConfluencePageSectionTool(Tool[PageSectionArgs]):
    def __init__(self, cfg: ConfluencePageSectionToolConfig, ctx: ExtensionContext) -> None: ...
    def tool_id(self): return ToolId("confluence_page_section")
    def tool_source_id(self): return ToolSourceId("plugin.confluence")
    def definition(self): return self._cfg.prompt.apply(ObjectSchema[PageSectionArgs](...))
    def execute(self, ctx, args): ...
```

`definition()` каждый раз создаёт новый `ObjectSchema` — стоимость
малая (литерал из dataclass-инстансов). Если для конкретного Tool это
станет горячим путём — кэшируется через `@cached_property` локально,
не как часть конвенции.

### 6.2. Tool-DTO (рядом с Tool)

Каждый Tool сопровождает свой dataclass-DTO — он нужен для типизации
`__init__`. Никаких `*_TOOL_SCHEMA` module-level констант **нет**:
schema для materializer'а Plugin собирает в `config()` плоской — общие
поля плюс per-tool `PromptOverlay` (§6.3). Tool-DTO конструируется в
`build()` из общих полей и соответствующего overlay'я.

```python
@dataclass(frozen=True)
class ConfluenceSearchToolConfig:
    base_url: str
    auth_token: str
    timeout_sec: float
    prompt: PromptOverlay


@dataclass(frozen=True)
class ConfluencePageToolConfig:
    base_url: str
    auth_token: str
    timeout_sec: float
    prompt: PromptOverlay


@dataclass(frozen=True)
class ConfluencePageSectionToolConfig:
    base_url: str
    auth_token: str
    timeout_sec: float
    prompt: PromptOverlay
```

### 6.3. Plugin — config() и build()

Plugin DTO — **плоский**: общие поля (`base_url`, `auth_token`,
`timeout_sec`) лежат на корне, per-tool prompt overlay'и — отдельными
полями с именем = tool. Без вложенных Tool-DTO в Plugin DTO.
`build()` собирает каждый Tool-DTO из общих полей и соответствующего
overlay'я.

```python
@dataclass(frozen=True)
class ConfluencePluginConfig:
    base_url: str
    auth_token: str
    timeout_sec: float
    confluence_search:       PromptOverlay
    confluence_page:         PromptOverlay
    confluence_page_section: PromptOverlay


class ConfluencePlugin:
    NAME = StrId("confluence")

    @classmethod
    def config(cls) -> ObjectSchema[ConfluencePluginConfig]:
        return ObjectSchema(
            description="Confluence multi-tool plugin config.",
            fields=[
                *ConfluenceConnection.fields(),    # base_url, auth_token, timeout_sec
                prompt_field("confluence_search"),
                prompt_field("confluence_page"),
                prompt_field("confluence_page_section"),
            ],
            factory=ConfluencePluginConfig,
        )

    @classmethod
    def build(cls, cfg: ConfluencePluginConfig, ctx: ExtensionContext) -> ToolSource:
        return StaticToolSource(
            id=ToolSourceId(f"plugin.{cls.NAME}"),
            priority=0,
            tools=[
                ConfluenceSearchTool(
                    ConfluenceSearchToolConfig(
                        base_url=cfg.base_url, auth_token=cfg.auth_token,
                        timeout_sec=cfg.timeout_sec, prompt=cfg.confluence_search,
                    ),
                    ctx,
                ),
                ConfluencePageTool(
                    ConfluencePageToolConfig(
                        base_url=cfg.base_url, auth_token=cfg.auth_token,
                        timeout_sec=cfg.timeout_sec, prompt=cfg.confluence_page,
                    ),
                    ctx,
                ),
                ConfluencePageSectionTool(
                    ConfluencePageSectionToolConfig(
                        base_url=cfg.base_url, auth_token=cfg.auth_token,
                        timeout_sec=cfg.timeout_sec, prompt=cfg.confluence_page_section,
                    ),
                    ctx,
                ),
            ],
        )
```

`config()` — одна плоская `ObjectSchema`: общие connection-поля + по
одному `prompt_field(name)` на каждый tool. `build()` явно
конструирует Tool-DTO из общих полей и соответствующего overlay'я.
Связь «какой Tool под каким overlay» определяется именем поля.

### 6.4. TOML

```toml
[tool.confluence]
enable      = true
base_url    = "@{base.confluence.base_url}"
auth_token  = "@{base.confluence.auth_token}"
timeout_sec = 30

[tool.confluence.confluence_search]
description = "Полнотекстовый поиск страниц Confluence — для своих пространств."

[tool.confluence.confluence_search.fields]
query = "Поисковый запрос."
limit = "Максимум hits."

[tool.confluence.confluence_page]
description = "Получить outline страницы Confluence."

[tool.confluence.confluence_page_section]
description = "Извлечь конкретную секцию страницы."
```

Connection указан **один раз** на корне `[tool.confluence]`. Под каждым
tool-блоком — только `PromptOverlay` (`description`, `fields.<name>`).
Никакого дублирования ключей.

### 6.5. Plugin с одним tool — тот же контракт

```python
@dataclass(frozen=True)
class ConfluenceSearchPluginConfig:
    base_url: str
    auth_token: str
    timeout_sec: float
    confluence_search: PromptOverlay


class ConfluenceSearchPlugin:
    NAME = StrId("confluence_search")

    @classmethod
    def config(cls) -> ObjectSchema[ConfluenceSearchPluginConfig]:
        return ObjectSchema(
            fields=[
                *ConfluenceConnection.fields(),
                prompt_field("confluence_search"),
            ],
            factory=ConfluenceSearchPluginConfig,
        )

    @classmethod
    def build(cls, cfg, ctx) -> ToolSource:
        return StaticToolSource(
            id=ToolSourceId(f"plugin.{cls.NAME}"),
            priority=0,
            tools=[
                ConfluenceSearchTool(
                    ConfluenceSearchToolConfig(
                        base_url=cfg.base_url, auth_token=cfg.auth_token,
                        timeout_sec=cfg.timeout_sec, prompt=cfg.confluence_search,
                    ),
                    ctx,
                ),
            ],
        )
```

#### TOML

```toml
[tool.confluence_search]
enable      = true
base_url    = "@{base.confluence.base_url}"
auth_token  = "@{base.confluence.auth_token}"
timeout_sec = 30

[tool.confluence_search.confluence_search]
description = "..."
```

Connection — один раз на корне. Под `[…confluence_search]` — только
`PromptOverlay`. Plugin name и tool name могут различаться — тогда путь
читается естественнее (`[tool.search.confluence_search]`).

### 6.6. Source-Plugin (MCP, remote)

Plugin не конструирует Tool-инстансы — `config()` возвращает schema
параметров MCP-сервера, `build()` собирает специализированный
`ToolSource`.

```python
class McpToolsPlugin:
    NAME = StrId("mcp")

    @classmethod
    def config(cls) -> ObjectSchema[McpServerConfig]:
        return MCP_SERVER_SCHEMA

    @classmethod
    def build(cls, cfg: McpServerConfig, ctx: ExtensionContext) -> ToolSource:
        return McpToolSource(cfg=cfg, ...)
```

Контракт `Plugin` соблюдается полностью: NAME, config, build → ToolSource.
App-цикл одинаков для всех.

## 7. Жизненный цикл `build_app`

```python
def build_app(argv: list[str], envfile: Path | None) -> RunningApp:
    bundle = ConfigBundle.from_sources([
        CliSource(argv=argv),
        EnvFileSource(envfile),
        EnvSource(),
        TomlFileSource(...),
        TomlSource(...),
    ])

    # Каждый DTO объявляет `SCHEMA: ClassVar[ObjectSchema[Self]]`,
    # bundle.get(DTO, prefix) — синтаксический сахар над materialize.
    core       = bundle.get(AppCoreConfig,    "core")
    agent      = bundle.get(AgentConfig,      "agent")
    workspaces = bundle.get(WorkspaceLayout,  "workspaces")
    ...

    ctx = ExtensionContext(...)
    tools_service = _build_tools_service(bundle, ctx)

    return RunningApp(
        core=core, agent=agent, workspaces=workspaces,
        tools_service=tools_service,
    )


def _build_tools_service(bundle: ConfigBundle, ctx: ExtensionContext) -> ToolsService:
    return ToolsService.from_sources(_install_plugins(bundle, discover_plugins(), ctx))


def _install_plugins(
    bundle: ConfigBundle,
    plugin_classes: Iterable[type[Plugin]],
    ctx: ExtensionContext,
) -> Iterable[ToolSource]:
    for plugin_cls in plugin_classes:
        mount = _mount_path_for(plugin_cls.NAME)
        if not _is_enabled(bundle, mount):
            continue
        cfg = bundle.materialize(plugin_cls.config(), mount)
        yield plugin_cls.build(cfg, ctx)
```

App работает только через `Plugin`-протокол: `NAME`, `config()`, `build()`.

## 8. Сохраняется без изменений

* `ConfigPath`, `ConfigValue`, `ConfigSpace`, `ConfigSource`, `ConfigLookup`.
* `ObjectSchema`, `FieldSpec`, `CollectionField`, `ItemReader`,
  `ScalarItem`, `ObjectItem`, коерсеры, инварианты. В `boba.declaration`
  добавляется один новый `FieldKind` — `NestedField` (см. §2.3).
* **`Tool` (`boba.tools.domain.tool`) — без изменений.** Только меняется
  convention использования (см. §6.1): Tool принимает готовый config-DTO
  с `prompt: PromptOverlay`, `definition()` строит `ObjectSchema` локально
  и заворачивает её в `cfg.prompt.apply(...)`.
* Все источники: `CliSource`, `EnvSource`, `EnvFileSource`, `TomlSource`,
  `TomlFileSource`. Ссылки разрешаются после слияния.
* `FlatConfigMaterializer` — к моменту материализации все ссылки разрешены.
* `FlatConfig.values`, `lookup`, `keys_under`, `child_segments`, `subtree`.
  Меняется только тип `origins`.
* `ToolsService`, `ToolFactory`, `ToolCatalog`, `ToolSource`,
  `StaticToolSource`, `ToolSourceId` (`boba.tools.framework.registry`).
  В `ToolsService` меняется только `__init__` (принимает `ToolCatalog`)
  и удаляется `rebuild_catalog()` — см. §2.10.

## 9. Открытые вопросы

### 9.1. Mount-convention для не-tool плагинов и форма entry-points

Для tool-плагинов: `tool.<name>`. Для pipelines / providers — варианты
свой ветки `pipeline.<name>`, единая `tool.<name>`, отдельные entry-point
group по роли.

### 9.2. Содержимое `ExtensionContext`

Что нужно дополнительно (logger, метрики, observers) — определится по
факту в PR-3. На старте может быть пустым dataclass.

### 9.3. Проверка dotenv-парсера на символ `@`

Используемый dotenv-загрузчик не должен интерпретировать `@{...}`.
Проверяется одной короткой пробой во время PR-2.

### 9.4. Расширение `PromptOverlay`

Сейчас overlay — `description` и `fields[name]`. Если позже понадобится
override'ить `examples`, ограничения, `enabled` per-param — `PromptOverlay`
расширяется и `apply` обновляется в одном месте.

### 9.5. Судьба класса `AppConfig`

Удалить полностью или превратить в неизменяемый dataclass-агрегат.
Рекомендация: первый вариант.

### 9.6. Дублирование connection-полей в TOML

Закрыто: Plugin DTO плоский, общие connection-поля на корне
`[tool.<plugin>]`, под каждым tool-блоком — только `PromptOverlay`.
Никакого дублирования ключей.

## 10. Миграция

### 10.1. Что удаляется

* `boba.config.section.ConfigSection` и все его подклассы.
* `boba.config.bootstrap.AppConfigBootstrap`.
* `boba.config.app.ConfigSectionFactory`, `_SectionProvider`,
  `SectionMissingError`, `AppConfig.section(...)`,
  `CONFIG_SECTIONS_ENTRY_POINT`.
* `AppConfig` — либо удаляется полностью, либо превращается в тонкий
  dataclass (см. §9.5).
* Entry-point group `boba.config_sections`.
* `register_tools(ctx)`-функции в `__init__.py` плагинов и связанная
  обвязка `ToolPluginLoader`/`ExtensionContext.config`.
* `param_desc`, `params_field`, `ParamOverlay`
  (`packages/boba-agent/src/boba/tools/domain/descriptions.py`) —
  заменяются на `prompt_field`, `PromptOverlay`, `PromptOverlay.apply`.
* Per-tool `ConfigSection`-классы с дублированием `Default(DEFAULT_*)` —
  заменяются на inline-`ObjectSchema` внутри `Plugin.config()` и одно
  поле в Plugin DTO. Никаких module-level `*_TOOL_SCHEMA` констант —
  схема плагина описана одной декларацией в одном месте.

### 10.2. Последовательность PR

1. **PR-1: `NestedField`.** `boba.declaration.NestedField` и одна ветка
   для него в `FlatConfigMaterializer._read_field`. ~10 строк +
   тесты. Ничего существующего не ломается.
2. **PR-2: reference resolver.** `boba.config.refs`, изменение типа
   `FlatConfig.origins` на `OriginChain`, фаза resolve в
   `ConfigBundle.from_sources`. Старая `ConfigSection`-инфра продолжает
   работать.
3. **PR-3: plugin-протокол + enable-convention + рефакторинг ToolsService.**
   Создаётся subpackage `packages/boba-core/src/boba/plugin/` с модулями
   `__init__.py` (`Plugin`, `ExtensionContext`) и `discovery.py`
   (`discover_plugins`). В composition root добавляются
   `_mount_path_for`, `_is_enabled` и второй путь установки плагинов.
   В `boba.tools.framework.registry`: `ToolsService.__init__` принимает
   `ToolCatalog`, удаляется `rebuild_catalog()`, добавляется classmethod
   `from_sources(sources)`. Tool не трогается.
4. **PR-4: prompt overlay.** `packages/boba-core/src/boba/plugin/prompt.py`:
   `PromptOverlay`, `PROMPT_OVERLAY_SCHEMA`, `prompt_field`.
5. **PR-5: один плагин (confluence)** переезжает на новую модель.
   Каждый Tool — чистый `__init__(cfg, ctx)`, `definition()` строит
   `ObjectSchema` локально и оборачивает в `cfg.prompt.apply(...)`.
   Plugin реализует `config()` и `build()`. Env переезжает на `[base]` +
   ссылки + prompt.
6. **PR-6: остальные плагины** (files, openai-adapter, prompt-providers, …).
7. **PR-7: core-секции** на прямой `bundle.materialize`. Решение по
   `AppConfig` (§9.5).
8. **PR-8: удаление старого слоя** (см. §10.1).

Каждый PR компилируется и проходит тесты, big-bang не нужен.
