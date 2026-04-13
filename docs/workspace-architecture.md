# Workspace Architecture — Design Document

## Обзор

Документ описывает архитектуру сервисов Workspace, разрабатываемых с нуля. Сервисы максимально независимы друг от друга. Единственная точка связи — `WorkspaceId`, который одновременно является идентификатором и механизмом синхронизации жизненного цикла (ref count).

---

## Основные сущности

### WorkspaceId

Идентификатор workspace + атомарный счётчик активных сервисов.

```python
class WorkspaceId:
    def __init__(self, id: UUID) -> None:
        self._id = id
        self._count = 0
        self._lock = asyncio.Lock()

    @property
    def id(self) -> UUID:
        return self._id

    async def acquire(self) -> None:
        """Сервис начинает работу с workspace."""
        async with self._lock:
            self._count += 1

    async def release(self) -> None:
        """Сервис завершает работу с workspace."""
        async with self._lock:
            if self._count <= 0:
                raise RuntimeError("release без acquire")
            self._count -= 1

    @property
    def active(self) -> bool:
        """Есть ли активные сервисы."""
        return self._count > 0
```

**Роль:** единственная общая зависимость для всех сервисов. Каждый сервис при `enter()` делает `acquire()`, при `close()` — `release()`. `WorkspaceRegistry` проверяет `active` перед удалением.

### MessageId

- Идентификатор конкретного сообщения в истории чата (`UUID`).
- Назначается при добавлении `LLMMessage` в историю через `ChatHistoryService.add_message()`.
- `LLMMessage` сам по себе не содержит `MessageId` — это чистая доменная модель сообщения. `MessageId` — это идентификатор хранения, который присваивается сервисом истории.

### ToolCallArgs (Generic)

```python
TArgs = TypeVar("TArgs")

@dataclass(frozen=True)
class ToolCallArgs(Generic[TArgs]):
    """Базовый класс аргументов tool call. Каждый инструмент определяет свою конкретную реализацию."""
    pass

# Пример конкретной реализации:
@dataclass(frozen=True)
class SearchArgs(ToolCallArgs["SearchArgs"]):
    query: str
    limit: int = 10

@dataclass(frozen=True)
class CalcArgs(ToolCallArgs["CalcArgs"]):
    expression: str
```

### ToolCall

```python
@dataclass(frozen=True)
class ToolCall(Generic[TArgs]):
    """Один вызов инструмента, инициированный LLM."""
    id: str                        # уникальный id вызова (назначается LLM, например "call_abc123")
    name: str                      # имя инструмента
    arguments: TArgs               # типизированные аргументы конкретного инструмента

# Использование:
# ToolCall[SearchArgs](id="call_1", name="search", arguments=SearchArgs(query="..."))
# ToolCall[CalcArgs](id="call_2", name="calc", arguments=CalcArgs(expression="2+2"))
```

### LLMMessage — иерархия по ролям

Вместо одного класса с опциональными полями — отдельный подкласс для каждой роли.
Каждый подкласс содержит только релевантные поля.

```python
@dataclass(frozen=True)
class LLMMessage:
    """Базовый класс сообщения в диалоге."""
    content: str = ""


@dataclass(frozen=True)
class SystemMessage(LLMMessage):
    """Системное сообщение (инструкции для LLM)."""
    pass


@dataclass(frozen=True)
class UserMessage(LLMMessage):
    """Сообщение пользователя."""
    pass


@dataclass(frozen=True)
class AssistantMessage(LLMMessage):
    """Ответ ассистента. Может содержать вызовы инструментов."""
    tool_calls: list[ToolCall] = field(default_factory=list)


@dataclass(frozen=True)
class ToolMessage(LLMMessage):
    """Результат выполнения инструмента."""
    tool_call_id: str = ""   # на какой ToolCall.id отвечает это сообщение
```

**Пояснение `tool_call_id`:** LLM может вызвать несколько tools параллельно, каждый с уникальным `id`.
Когда мы возвращаем результат, `tool_call_id` связывает результат с конкретным вызовом:

```
AssistantMessage(tool_calls=[
    ToolCall(id="call_1", name="search", arguments=SearchArgs(query="...")),
    ToolCall(id="call_2", name="calc",   arguments=CalcArgs(expression="2+2")),
])

ToolMessage(tool_call_id="call_1", content="результат search")
ToolMessage(tool_call_id="call_2", content="результат calc")
```

### Модели промптов

Три типа данных, которые идут в разные параметры LLM API. Общего базового класса нет — сущности принципиально разные.

#### SystemPromptBlock

```python
@dataclass(frozen=True)
class SystemPromptBlock:
    """Один собранный блок системного промпта."""
    name: str          # "identity", "environment", "claude_md"
    content: str       # текст блока
```

#### Модели инструментов (Tools)

```python
# ---------------------------------------------------------------------------
# Параметры инструмента — каждый tool определяет свой dataclass
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ToolParams:
    """Базовый класс параметров. Каждый инструмент наследует свой."""
    pass

# Примеры:
@dataclass(frozen=True)
class ReadParams(ToolParams):
    file_path: str
    offset: int | None = None
    limit: int | None = None

@dataclass(frozen=True)
class SearchParams(ToolParams):
    query: str
    top_k: int = 5

@dataclass(frozen=True)
class BashParams(ToolParams):
    command: str
    timeout: int = 120000


# ---------------------------------------------------------------------------
# Схема параметров — типизированная, вместо dict
# ---------------------------------------------------------------------------

class JsonType(Enum):
    STRING = "string"
    INTEGER = "integer"
    NUMBER = "number"
    BOOLEAN = "boolean"

@dataclass(frozen=True)
class ParamSchema:
    """Описание одного параметра для LLM."""
    name: str
    type: JsonType
    description: str
    required: bool = True
    default: Any = None

@dataclass(frozen=True)
class ToolInputSchema:
    """Полная схема параметров инструмента."""
    params: list[ParamSchema]


# ---------------------------------------------------------------------------
# Определение инструмента — что видит LLM
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ToolDefinition:
    """Полное определение инструмента для передачи в API.
    НЕ текст — передаётся в параметр tools API."""
    name: str                      # "read_file", "bash"
    description: str               # что видит LLM
    input_schema: ToolInputSchema  # типизированная схема параметров


# ---------------------------------------------------------------------------
# Результат выполнения
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ToolResult:
    """Результат выполнения инструмента."""
    content: str
    is_error: bool = False


# ---------------------------------------------------------------------------
# Tool — абстрактный инструмент
# ---------------------------------------------------------------------------

TParams = TypeVar("TParams", bound=ToolParams)

class Tool(ABC, Generic[TParams]):
    """Один инструмент: определение + выполнение."""

    @property
    @abstractmethod
    def definition(self) -> ToolDefinition: ...

    @property
    @abstractmethod
    def params_type(self) -> type[TParams]: ...

    @abstractmethod
    async def execute(self, params: TParams) -> ToolResult: ...

    async def enter(self) -> None:
        """Инициализация. По умолчанию no-op."""
        pass

    async def close(self) -> None:
        """Освобождение ресурсов. По умолчанию no-op."""
        pass
```

#### UserPromptTemplate

```python
class Position(Enum):
    BEFORE = "before"
    AFTER = "after"

@dataclass(frozen=True)
class UserPromptTemplate:
    """Шаблон обогащения user message контекстом."""
    name: str             # "ide_selection", "git_status"
    template: str         # "<ide_selection>{content}</ide_selection>"
    position: Position    # куда вставлять относительно сообщения юзера
```

#### Куда что идёт в API

| Модель | Параметр API | Формат |
|---|---|---|
| `SystemPromptBlock` | `system` / `messages[role=system]` | текст (конкатенация блоков) |
| `ToolDefinition` | `tools` | `ToolInputSchema` (типизированная схема) |
| `UserPromptTemplate` | `messages[role=user]` | обёртка вокруг user message |

### ChatConfig

```python
@dataclass
class ChatConfig:
    model: str
    max_tokens: int
```

---

## Сервисы

### 1. WorkspaceRegistry

Управляет жизненным циклом workspace'ов: создание, получение, удаление.
Единственное место, знающее обо всех существующих workspace'ах.

```python
class WorkspaceBusyError(Exception):
    """Workspace нельзя удалить — есть активные сервисы."""
    workspace_id: WorkspaceId

class WorkspaceNotFoundError(Exception):
    """Workspace не найден."""
    pass

class WorkspaceRegistry(ABC):
    def create() -> WorkspaceId
        """Создать новый workspace, вернуть его WorkspaceId."""

    def get(id: UUID) -> WorkspaceId
        """Получить WorkspaceId существующего workspace.
        Raises WorkspaceNotFoundError."""

    async def delete(workspace: WorkspaceId) -> None
        """Удалить workspace и все его данные.
        Raises WorkspaceBusyError если workspace.active (есть работающие сервисы)."""
```

**DI:** singleton.

**Семантика delete:** Registry проверяет `workspace.active` — если счётчик > 0, бросает `WorkspaceBusyError`. Удаление возможно только когда все сервисы завершили работу (`close()`).

---

### 2. Базовый контракт сервисов — enter/close

Все сервисы, работающие с workspace, следуют единому протоколу жизненного цикла:

```python
class WorkspaceAwareService(ABC):
    """Базовый контракт для сервисов, привязанных к workspace."""

    def __init__(self, workspace: WorkspaceId) -> None:
        self._workspace = workspace

    async def enter(self) -> None:
        """Начать работу — acquire ref count."""
        await self._workspace.acquire()

    async def close(self) -> None:
        """Завершить работу — release ref count."""
        await self._workspace.release()
```

Каждый сервис создаётся независимо, получая `WorkspaceId` в конструктор. Сервисы не знают друг о друге.

---

### 3. ChatHistoryService

Единственное место для взаимодействия с историей чата. 1 workspace = 1 чат (плоский список сообщений).
Методы возвращают `Iterator` — ленивая загрузка, не держим всю историю в памяти.

```python
class ChatHistoryService(WorkspaceAwareService, ABC):
    def add_message(message: LLMMessage) -> MessageId
        """Добавить сообщение в историю."""

    def get_messages() -> Iterator[LLMMessage]
        """Итератор по всем сообщениям текущего чата."""

    def get_message(message_id: MessageId) -> LLMMessage
        """Получить конкретное сообщение."""

    def update_message(message_id: MessageId, message: LLMMessage) -> None
        """Обновить сообщение."""

    def delete_message(message_id: MessageId) -> None
        """Удалить сообщение из истории."""

    def clear() -> None
        """Очистить всю историю чата."""
```

---

### 4. ChatConfigService

Единственное место для работы с конфигурацией Workspace. Гранулярный доступ по отдельным параметрам.

```python
class ChatConfigService(WorkspaceAwareService, ABC):
    def get_config() -> ChatConfig
        """Получить полную конфигурацию."""

    def get(key: str) -> Any
        """Получить значение конкретного параметра."""

    def set(key: str, value: Any) -> None
        """Установить значение конкретного параметра."""

    def delete(key: str) -> None
        """Удалить параметр (сбросить к значению по умолчанию)."""

    def reset() -> None
        """Сбросить всю конфигурацию к значениям по умолчанию."""
```

---

### 5. SystemPromptService — композиция из провайдеров

System prompt собирается из множества независимых **провайдеров** (`SystemPromptProvider`).
Каждый провайдер знает откуда взять данные (файл, env, код) и имеет свой `priority`.

#### ProviderId

```python
class ProviderId:
    """Идентификатор провайдера."""

    def __init__(self, name: str) -> None:
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    def __eq__(self, other: object) -> bool:
        return isinstance(other, ProviderId) and self._name == other._name

    def __hash__(self) -> int:
        return hash(self._name)

    def __repr__(self) -> str:
        return f"ProviderId({self._name!r})"
```

#### SystemPromptProvider (abstract)

```python
class SystemPromptProvider(ABC):
    """Источник одного блока системного промпта."""

    @property
    @abstractmethod
    def id(self) -> ProviderId: ...

    @property
    @abstractmethod
    def priority(self) -> int: ...

    @abstractmethod
    async def build(self) -> SystemPromptBlock: ...

    async def enter(self) -> None:
        """Инициализация провайдера. По умолчанию no-op."""
        pass

    async def close(self) -> None:
        """Освобождение ресурсов. По умолчанию no-op."""
        pass
```

#### Конкретные реализации провайдеров

```python
class StaticPromptProvider(SystemPromptProvider):
    """Фиксированный текст, зашитый в код (identity, rules, tone)."""

    def __init__(self, id: ProviderId, priority: int, content: str) -> None:
        self._id = id
        self._priority = priority
        self._content = content

    @property
    def id(self) -> ProviderId:
        return self._id

    @property
    def priority(self) -> int:
        return self._priority

    async def build(self) -> SystemPromptBlock:
        return SystemPromptBlock(name=self.id.name, content=self._content)


class FilePromptProvider(SystemPromptProvider):
    """Читает блок из файла на диске (BOBA.md, MEMORY.md и т.д.).
    Держит ref count на workspace — защищает от удаления пока провайдер активен.
    Если файла нет — возвращает default_prompt или пустой блок."""

    def __init__(self, id: ProviderId, priority: int,
                 workspace: WorkspaceId,
                 folder: Path, file_name: str,
                 default_prompt: str = "") -> None:
        self._id = id
        self._priority = priority
        self._workspace = workspace
        self._folder = folder
        self._file_name = file_name
        self._default_prompt = default_prompt

    @property
    def id(self) -> ProviderId:
        return self._id

    @property
    def priority(self) -> int:
        return self._priority

    @property
    def path(self) -> Path:
        return self._folder / self._file_name

    async def enter(self) -> None:
        await self._workspace.acquire()

    async def close(self) -> None:
        await self._workspace.release()

    async def build(self) -> SystemPromptBlock:
        if self.path.exists():
            content = self.path.read_text()
        else:
            content = self._default_prompt
        return SystemPromptBlock(name=self.id.name, content=content)


class EnvironmentPromptProvider(SystemPromptProvider):
    """Информация о среде выполнения: OS, shell, platform, model, дата."""

    def __init__(self) -> None:
        self._id = ProviderId("environment")
        self._priority = 60

    @property
    def id(self) -> ProviderId:
        return self._id

    @property
    def priority(self) -> int:
        return self._priority

    async def build(self) -> SystemPromptBlock:
        lines = [
            f"Platform: {platform.system()}",
            f"Shell: {os.environ.get('SHELL', 'unknown')}",
            f"OS Version: {platform.release()}",
            f"Current date: {date.today().isoformat()}",
        ]
        return SystemPromptBlock(name=self.id.name, content="\n".join(lines))


class GitPromptProvider(SystemPromptProvider):
    """Текущее состояние git: branch, status, recent commits."""

    def __init__(self) -> None:
        self._id = ProviderId("git_status")
        self._priority = 80

    @property
    def id(self) -> ProviderId:
        return self._id

    @property
    def priority(self) -> int:
        return self._priority

    async def build(self) -> SystemPromptBlock:
        branch = await run("git branch --show-current")
        status = await run("git status --short")
        log = await run("git log --oneline -5")
        content = (
            f"Current branch: {branch}\n\n"
            f"Status:\n{status}\n\n"
            f"Recent commits:\n{log}"
        )
        return SystemPromptBlock(name=self.id.name, content=content)


class IDEPromptProvider(SystemPromptProvider):
    """Инструкции, специфичные для IDE (VSCode, JetBrains).
    В CLI-режиме может отсутствовать."""

    def __init__(self, ide_type: str) -> None:
        self._id = ProviderId("ide")
        self._priority = 70
        self._ide_type = ide_type

    @property
    def id(self) -> ProviderId:
        return self._id

    @property
    def priority(self) -> int:
        return self._priority


class SkillsPromptProvider(SystemPromptProvider):
    """Формирует описание доступных skills (slash-commands) из реестра."""

    def __init__(self, skill_registry: SkillRegistry) -> None:
        self._id = ProviderId("skills")
        self._priority = 110
        self._skill_registry = skill_registry

    @property
    def id(self) -> ProviderId:
        return self._id

    @property
    def priority(self) -> int:
        return self._priority

    async def build(self) -> SystemPromptBlock:
        lines = ["Available skills:"]
        for skill in self._skill_registry:
            lines.append(f"- {skill.name}: {skill.description}")
        return SystemPromptBlock(name=self.id.name, content="\n".join(lines))


class DeferredToolsPromptProvider(SystemPromptProvider):
    """Список отложенных инструментов (MCP, загружаемые по запросу)."""

    def __init__(self, deferred_tools: list[str]) -> None:
        self._id = ProviderId("deferred_tools")
        self._priority = 120
        self._deferred_tools = deferred_tools

    @property
    def id(self) -> ProviderId:
        return self._id

    @property
    def priority(self) -> int:
        return self._priority

    async def build(self) -> SystemPromptBlock:
        content = "Deferred tools available via ToolSearch:\n"
        content += "\n".join(f"- {name}" for name in self._deferred_tools)
        return SystemPromptBlock(name=self.id.name, content=content)


class CallbackPromptProvider(SystemPromptProvider):
    """Произвольная логика через callback (для нестандартных источников)."""

    def __init__(self, id: ProviderId, priority: int,
                 callback: Callable[[], Awaitable[str]]) -> None:
        self._id = id
        self._priority = priority
        self._callback = callback

    @property
    def id(self) -> ProviderId:
        return self._id

    @property
    def priority(self) -> int:
        return self._priority

    async def build(self) -> SystemPromptBlock:
        content = await self._callback()
        return SystemPromptBlock(name=self.id.name, content=content)
```

#### Таблица провайдеров (порядок сборки Claude Code)

| Priority | Provider | Источник | Когда меняется |
|---|---|---|---|
| 0 | `StaticPromptProvider("identity")` | код (константа) | при обновлении версии |
| 10 | `StaticPromptProvider("security")` | код (константа) | при обновлении версии |
| 20 | `StaticPromptProvider("tool_rules")` | код (константа) | при обновлении версии |
| 30 | `StaticPromptProvider("task_guide")` | код (константа) | при обновлении версии |
| 40 | `StaticPromptProvider("git_guide")` | код (константа) | при обновлении версии |
| 50 | `StaticPromptProvider("tone")` | код (константа) | при обновлении версии |
| 60 | `EnvironmentPromptProvider` | OS, runtime | при каждом `build()` |
| 70 | `IDEPromptProvider` | тип IDE из контекста | при старте сессии |
| 80 | `GitPromptProvider` | git CLI | при каждом `build()` |
| 90 | `FilePromptProvider("claude_md")` | `BOBA.md` на диске | при изменении файла |
| 100 | `FilePromptProvider("memory")` | `MEMORY.md` на диске | при изменении файла |
| 110 | `SkillsPromptProvider` | реестр skills | при регистрации skill |
| 120 | `DeferredToolsPromptProvider` | список deferred tools | при подключении MCP |

#### SystemPromptResult

```python
class SystemPromptResult:
    """Результат сборки system prompt."""

    def __init__(self, blocks: Iterator[SystemPromptBlock]) -> None:
        self._blocks = blocks

    def build(self) -> str:
        """Конкатенация всех непустых блоков через двойной перенос строки."""
        return "\n\n".join(b.content for b in self._blocks if b.content)

    def __iter__(self) -> Iterator[SystemPromptBlock]:
        """Итератор по отдельным блокам (для отладки/UI)."""
        return iter(self._blocks)
```

#### SystemPromptService

```python
class SystemPromptService:
    """Реестр провайдеров + сборка итогового system prompt.
    Не привязан к workspace напрямую — workspace-зависимость
    определяется конкретными провайдерами через их enter()/close()."""

    async def register(provider: SystemPromptProvider) -> None
        """Зарегистрировать провайдер и вызвать provider.enter()."""

    async def unregister(id: ProviderId) -> None
        """Вызвать provider.close() и убрать провайдер."""

    def providers() -> Iterator[SystemPromptProvider]
        """Все зарегистрированные провайдеры (отсортированы по priority)."""

    async def build() -> SystemPromptResult
        """Собрать system prompt из всех провайдеров (по priority).
        Результат: result.build() для API, iter(result) для отладки."""

    async def close() -> None
        """Вызвать close() у всех зарегистрированных провайдеров."""
```

#### Bootstrapping

```python
sps = SystemPromptService()

# Статика — enter() no-op
await sps.register(StaticPromptProvider(ProviderId("identity"),   0,  IDENTITY_TEXT))
await sps.register(StaticPromptProvider(ProviderId("security"),   10, SECURITY_TEXT))
await sps.register(StaticPromptProvider(ProviderId("tool_rules"), 20, TOOL_RULES_TEXT))
await sps.register(StaticPromptProvider(ProviderId("task_guide"), 30, TASK_GUIDE_TEXT))
await sps.register(StaticPromptProvider(ProviderId("git_guide"),  40, GIT_GUIDE_TEXT))
await sps.register(StaticPromptProvider(ProviderId("tone"),       50, TONE_TEXT))

# Runtime — enter() no-op
await sps.register(EnvironmentPromptProvider())
await sps.register(IDEPromptProvider(ide_type="vscode"))
await sps.register(GitPromptProvider())

# Файлы — enter() вызывает workspace.acquire()
await sps.register(FilePromptProvider(ProviderId("boba_md"), 90,  workspace, ws_path,     "BOBA.md"))
await sps.register(FilePromptProvider(ProviderId("memory"),  100, workspace, memory_path, "MEMORY.md"))

# Динамические реестры — id и priority зашиты в класс
sps.register(SkillsPromptProvider(skill_registry))
sps.register(DeferredToolsPromptProvider(deferred_tools))

# Сборка — вызывается при каждом запросе к LLM
result = await sps.build()

result.build()                           # → полный system prompt для API
for block in result:                     # → итерация для отладки
    ...

# Управление
sps.unregister(GitPromptProvider.ID)     # убрать по ProviderId
```

---

### 6. ToolsService

Реестр инструментов + диспетчеризация выполнения. Не привязан к workspace напрямую — workspace-зависимость определяется конкретными `Tool`'ами через их `enter()`/`close()`.

```python
class ToolsService:
    """Реестр инструментов: регистрация, определения для API, выполнение."""

    async def register(tool: Tool) -> None
        """Зарегистрировать инструмент, вызвать tool.enter()."""

    async def unregister(name: str) -> None
        """Вызвать tool.close() и убрать инструмент."""

    def list() -> Iterator[Tool]
        """Все зарегистрированные инструменты."""

    def get_definitions() -> Iterator[ToolDefinition]
        """Определения всех инструментов для передачи в параметр tools API."""

    async def execute(name: str, raw_args: dict[str, Any]) -> ToolResult
        """Найти tool по имени, сконструировать типизированные params
        из raw JSON (через tool.params_type), выполнить tool.execute()."""

    async def close() -> None
        """Вызвать close() у всех зарегистрированных инструментов."""
```

---

### 7. UserPromptService

Управление шаблонами обогащения пользовательских сообщений.

```python
class UserPromptService(WorkspaceAwareService, ABC):
    """CRUD шаблонов + обогащение user message."""

    def register(template: UserPromptTemplate) -> None
        """Зарегистрировать шаблон."""

    def unregister(name: str) -> None
        """Убрать шаблон по имени."""

    def list() -> Iterator[UserPromptTemplate]
        """Все зарегистрированные шаблоны."""

    def enrich_message(user_text: str) -> str
        """Обернуть сообщение юзера шаблонами (BEFORE/AFTER по position)."""
```

---

## Интеграция с DI (dishka)

### Скоупы

| Scope | Что живёт | Lifetime |
|---|---|---|
| `Scope.APP` | `WorkspaceRegistry` | singleton, весь процесс |
| `Scope.REQUEST` | Все workspace-сервисы | один запрос / сессия |

### WorkspaceId как контекст scope

`WorkspaceId` передаётся через `from_context` при входе в REQUEST scope. Это связывает все сервисы внутри scope с одним workspace.

### enter/close через yield-провайдеры

Dishka поддерживает финализаторы через `yield` в `@provide`. Это позволяет привязать `enter()`/`close()` к lifecycle scope — ref count управляется автоматически:

```python
# ---------------------------------------------------------------------------
# APP scope — singleton
# ---------------------------------------------------------------------------

class WorkspaceProvider(Provider):
    scope = Scope.APP

    @provide
    def registry(self) -> WorkspaceRegistry:
        return FsWorkspaceRegistry()


# ---------------------------------------------------------------------------
# REQUEST scope — per-workspace сервисы
# ---------------------------------------------------------------------------

class WorkspaceServicesProvider(Provider):
    scope = Scope.REQUEST

    # WorkspaceId передаётся как контекст при входе в scope
    workspace_id = from_context(provides=WorkspaceId, scope=Scope.REQUEST)

    @provide
    async def chat_history(self, ws: WorkspaceId) -> AsyncIterator[ChatHistoryService]:
        svc = FsChatHistoryService(ws)
        await svc.enter()       # acquire ref count
        yield svc
        await svc.close()       # release ref count (финализатор scope)

    @provide
    async def chat_config(self, ws: WorkspaceId) -> AsyncIterator[ChatConfigService]:
        svc = FsChatConfigService(ws)
        await svc.enter()
        yield svc
        await svc.close()

    @provide
    async def system_prompt_service(self, ws: WorkspaceId) -> AsyncIterator[SystemPromptService]:
        svc = SystemPromptService()

        # Bootstrapping — register() вызывает provider.enter()
        await svc.register(StaticPromptProvider(ProviderId("identity"),   0,  IDENTITY_TEXT))
        await svc.register(StaticPromptProvider(ProviderId("security"),   10, SECURITY_TEXT))
        await svc.register(StaticPromptProvider(ProviderId("tool_rules"), 20, TOOL_RULES_TEXT))
        await svc.register(StaticPromptProvider(ProviderId("task_guide"), 30, TASK_GUIDE_TEXT))
        await svc.register(StaticPromptProvider(ProviderId("git_guide"),  40, GIT_GUIDE_TEXT))
        await svc.register(StaticPromptProvider(ProviderId("tone"),       50, TONE_TEXT))
        await svc.register(EnvironmentPromptProvider())
        await svc.register(IDEPromptProvider(ide_type="vscode"))
        await svc.register(GitPromptProvider())
        await svc.register(FilePromptProvider(ProviderId("boba_md"), 90,  ws, ws_path,     "BOBA.md"))
        await svc.register(FilePromptProvider(ProviderId("memory"),  100, ws, memory_path, "MEMORY.md"))

        yield svc
        await svc.close()  # вызывает close() у всех провайдеров

    @provide
    async def tools_service(self, ws: WorkspaceId) -> AsyncIterator[ToolsService]:
        svc = ToolsService()

        # Bootstrapping — register() вызывает tool.enter()
        # await svc.register(ReadFileTool(...))
        # await svc.register(SearchDocumentsTool(...))
        # ...

        yield svc
        await svc.close()  # вызывает close() у всех tools

    @provide
    async def user_prompt_service(self, ws: WorkspaceId) -> AsyncIterator[UserPromptService]:
        svc = UserPromptService()
        yield svc
```

**Ключевые свойства:**

- `enter()` вызывается автоматически при первом resolve сервиса в scope
- `close()` вызывается автоматически при закрытии scope (через `yield` финализатор)
- Невозможно забыть вызвать `close()` — dishka гарантирует вызов финализатора
- Ref count полностью автоматический
- Добавление нового сервиса — новый `@provide` с yield, ни Registry ни другие провайдеры не меняются

---

## Модель взаимодействия

### Сценарий: обработка запроса пользователя

```
UI/API Request (workspace_uuid, user_message)
  │
  ▼
registry = container.get(WorkspaceRegistry)      # APP scope
workspace = registry.get(workspace_uuid)          # → WorkspaceId
  │
  ▼
async with container(context={WorkspaceId: workspace}) as scope:
    │
    │  # dishka резолвит сервисы, enter() вызван автоматически
    │
    ├── config  = await scope.get(ChatConfigService)
    ├── history = await scope.get(ChatHistoryService)
    ├── sps     = await scope.get(SystemPromptService)
    ├── tools   = await scope.get(ToolsService)
    ├── ups     = await scope.get(UserPromptService)
    │
    │  # Сборка контекста
    ├── sys_result    = await sps.build()                → SystemPromptResult
    ├── tool_defs     = tools.get_definitions()          → Iterator[ToolDefinition]
    ├── messages      = history.get_messages()           → Iterator[LLMMessage]
    ├── enriched_msg  = ups.enrich_message(user_message) → str
    ├── chat_config   = config.get_config()              → ChatConfig
    │
    ▼
  LLM Call:
    system=sys_result.build(),
    tools=tool_defs,
    messages=[...messages, UserMessage(content=enriched_msg)],
    model=chat_config.model,
    max_tokens=chat_config.max_tokens,
    │
    ▼
  # LLM может вернуть tool_calls → выполнение через tools.execute()
  for tool_call in assistant_response.tool_calls:
      result = await tools.execute(tool_call.name, tool_call.arguments)
      # result: ToolResult(content=..., is_error=...)
    │
    ▼
  history.add_message(UserMessage(content=enriched_msg))
  history.add_message(AssistantMessage(content=..., tool_calls=[...]))

# scope закрылся → close() вызван для всех сервисов → ref count == 0
```

### Сценарий: создание нового workspace

```
registry = container.get(WorkspaceRegistry)
workspace = registry.create()  → WorkspaceId

# настройка через scope
async with container(context={WorkspaceId: workspace}) as scope:
    config = await scope.get(ChatConfigService)
    config.set("model", default_model)
# scope закрылся → close() → ref count == 0
```

### Сценарий: удаление workspace

```
registry = container.get(WorkspaceRegistry)
workspace = registry.get(workspace_uuid)

await registry.delete(workspace)
  │
  ├── workspace.active == True  → WorkspaceBusyError (открыт scope)
  └── workspace.active == False → удаление данных, ok
```

### Сценарий: настройка workspace через UI

```
async with container(context={WorkspaceId: workspace}) as scope:
    config = await scope.get(ChatConfigService)
    sps    = await scope.get(SystemPromptService)

    config.get_config()                → показать текущие значения
    config.set("model", "...")         → пользователь выбрал модель
    config.set("max_tokens", 4096)

    sps.providers()                    → показать зарегистрированные провайдеры
    await sps.build_blocks()           → показать блоки system prompt для отладки
# scope закрылся → всё освобождено
```

---

## Схема зависимостей

```
┌─ Scope.APP ──────────────────────────────────────────────────┐
│                                                              │
│  WorkspaceRegistry (singleton)                               │
│    │                                                         │
│    │  create() / get(uuid) / delete()                        │
│    ▼                                                         │
│  WorkspaceId (UUID + ref count)                              │
│                                                              │
└──────────────────────────────────────────────────────────────┘
        │
        │  from_context(provides=WorkspaceId)
        ▼
┌─ Scope.REQUEST ──────────────────────────────────────────────┐
│                                                              │
│  yield-провайдеры (auto enter/close):                        │
│                                                              │
│  ├── ChatHistoryService        ◄── LLMMessage (иерархия)     │
│  │                                  ├── SystemMessage        │
│  │                                  ├── UserMessage          │
│  │                                  ├── AssistantMessage     │
│  │                                  └── ToolMessage          │
│  │                                                           │
│  ├── ChatConfigService         ◄── ChatConfig                │
│  │                                                           │
│  ├── SystemPromptService       ◄── SystemPromptProvider's    │
│  │    (реестр провайдеров)          ├── StaticPromptProvider  │
│  │    build() → str                 ├── FilePromptProvider    │
│  │                                  ├── EnvironmentProvider   │
│  │                                  ├── GitPromptProvider     │
│  │                                  ├── IDEPromptProvider     │
│  │                                  ├── SkillsProvider       │
│  │                                  └── DeferredToolsProvider│
│  │                                                           │
│  ├── ToolsService              ◄── Tool[TParams]             │
│  │    get_definitions()             ├── ToolDefinition       │
│  │    execute(name, args)           │    (name + description │
│  │    → ToolResult                  │     + ToolInputSchema) │
│  │                                  ├── ToolParams (иерархия)│
│  │                                  └── ToolResult           │
│  │                                                           │
│  └── UserPromptService         ◄── UserPromptTemplate        │
│       enrich_message(text)          (template + position)    │
│       → str                                                  │
│                                                              │
└──────────────────────────────────────────────────────────────┘
```

---

## Принципы

1. **WorkspaceId — единственная точка связи.** Сервисы не знают друг о друге. Каждый получает `WorkspaceId` через DI.
2. **Ref count — автоматический через dishka scope.** `yield`-провайдеры вызывают `enter()` при создании и `close()` при закрытии scope. Невозможно забыть освободить ресурсы.
3. **Registry — владелец.** Создаёт, хранит, удаляет workspace'ы. Удаление безопасно: только когда `active == False` (все scope'ы закрыты).
4. **Сервисы создаются независимо** — каждый в своём `@provide`. Добавление нового сервиса — новый yield-провайдер, ни Registry ни другие провайдеры не меняются.
5. **WorkspaceAwareService** — базовый контракт `enter()/close()` для всех сервисов, работающих с workspace.

---

## Статус

- [ ] Реализовать `WorkspaceId` (UUID + ref count)
- [ ] Определить интерфейс `WorkspaceRegistry`
- [ ] Определить `WorkspaceAwareService` (базовый enter/close)
- [ ] Определить модели: `LLMMessage` (иерархия), `ToolCall`, `ChatConfig`
- [ ] Определить модели промптов: `SystemPromptBlock`, `UserPromptTemplate`
- [ ] Определить модели инструментов: `ToolParams`, `ParamSchema`, `ToolInputSchema`, `ToolDefinition`, `ToolResult`, `Tool`
- [ ] Определить интерфейс `ChatHistoryService`
- [ ] Определить интерфейс `ChatConfigService`
- [ ] Определить `SystemPromptProvider` (abstract) и конкретные реализации
- [ ] Определить `SystemPromptService` (реестр провайдеров + сборка)
- [ ] Определить `ToolsService` (реестр + диспетчеризация)
- [ ] Определить `UserPromptService`
- [ ] Настроить dishka-провайдеры (APP + REQUEST scope с yield + bootstrapping)
- [ ] Реализовать Fs-имплементации
