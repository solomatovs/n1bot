# Workspace Architecture — Design Document

## Обзор

Документ описывает архитектуру сервисов Workspace, разрабатываемых с нуля. Сервисы максимально независимы друг от друга. Единственная точка связи — `WorkspaceId`, который одновременно является идентификатором и механизмом синхронизации жизненного цикла (ref count).

---

## 1. Workspace

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

### WorkspaceAwareService

Базовый контракт для сервисов, привязанных к workspace.

```python
class WorkspaceAwareService(ABC):
    def __init__(self, workspace: WorkspaceId) -> None:
        self._workspace = workspace

    async def enter(self) -> None:
        """Начать работу — acquire ref count."""
        await self._workspace.acquire()

    async def close(self) -> None:
        """Завершить работу — release ref count."""
        await self._workspace.release()
```

### WorkspaceRegistry

Управляет жизненным циклом workspace'ов: создание, получение, удаление.

```python
class WorkspaceBusyError(Exception):
    workspace_id: WorkspaceId

class WorkspaceNotFoundError(Exception):
    pass

class WorkspaceRegistry(ABC):
    def create() -> WorkspaceId
    def get(id: UUID) -> WorkspaceId
    async def delete(workspace: WorkspaceId) -> None
        """Raises WorkspaceBusyError если workspace.active."""
```

**DI:** singleton. Проверяет `workspace.active` перед удалением.

---

## 2. Chat

### LLMMessage — иерархия по ролям

```python
@dataclass(frozen=True)
class LLMMessage:
    """Базовый класс сообщения в диалоге."""
    content: str = ""

@dataclass(frozen=True)
class SystemMessage(LLMMessage):
    pass

@dataclass(frozen=True)
class UserMessage(LLMMessage):
    pass

@dataclass(frozen=True)
class AssistantMessage(LLMMessage):
    tool_calls: list[ToolCall] = field(default_factory=list)

@dataclass(frozen=True)
class ToolMessage(LLMMessage):
    tool_call_id: str = ""
```

### ToolCall

`ToolCall` — конкретный вызов инструмента от LLM. Живёт в `AssistantMessage.tool_calls`.
`ToolParams` (определён в секции Tools) — базовый класс аргументов.

```python
TParams = TypeVar("TParams", bound=ToolParams)

@dataclass(frozen=True)
class ToolCall(Generic[TParams]):
    id: str            # уникальный id вызова (назначается LLM)
    name: str          # имя инструмента
    arguments: TParams # типизированные аргументы (см. секцию Tools)
```

**Пояснение `tool_call_id`:** LLM может вызвать несколько tools параллельно. `ToolMessage.tool_call_id` связывает результат с конкретным `ToolCall.id`.

### MessageId

- UUID, назначается `ChatHistoryService.add_message()`.
- `LLMMessage` не содержит `MessageId` — это идентификатор хранения.

### ChatConfig

```python
@dataclass
class ChatConfig:
    model: str
    max_tokens: int
```

### ChatHistoryService

1 workspace = 1 чат (плоский список сообщений).

```python
class ChatHistoryService(WorkspaceAwareService, ABC):
    def add_message(message: LLMMessage) -> MessageId
    def get_messages() -> Iterator[LLMMessage]
    def get_message(message_id: MessageId) -> LLMMessage
    def update_message(message_id: MessageId, message: LLMMessage) -> None
    def delete_message(message_id: MessageId) -> None
    def clear() -> None
```

### ChatConfigService

Гранулярный доступ по отдельным параметрам.

```python
class ChatConfigService(WorkspaceAwareService, ABC):
    def get_config() -> ChatConfig
    def get(key: str) -> Any
    def set(key: str, value: Any) -> None
    def delete(key: str) -> None
    def reset() -> None
```

---

## 3. System Prompt

System prompt собирается из множества независимых провайдеров. Каждый провайдер знает откуда взять данные (файл, env, код) и имеет свой `priority`.

### SystemPromptBlock

```python
@dataclass(frozen=True)
class SystemPromptBlock:
    """Один собранный блок системного промпта."""
    name: str
    content: str
```

### ProviderId

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

### SystemPromptProvider (abstract)

```python
class SystemPromptProvider(ABC):
    @property
    @abstractmethod
    def id(self) -> ProviderId: ...

    @property
    @abstractmethod
    def priority(self) -> int: ...

    @abstractmethod
    async def build(self) -> SystemPromptBlock: ...

    async def enter(self) -> None:
        pass

    async def close(self) -> None:
        pass
```

### Конкретные реализации провайдеров

```python
class StaticPromptProvider(SystemPromptProvider):
    """Фиксированный текст, зашитый в код."""

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
    """Читает блок из файла на диске.
    Держит ref count на workspace — защищает от удаления."""

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
    """Информация о среде выполнения."""

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
    """Текущее состояние git."""

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
    """Инструкции, специфичные для IDE."""

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
    """Описание доступных skills (slash-commands)."""

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
    """Список отложенных инструментов (MCP)."""

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
    """Произвольная логика через callback."""

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

### Таблица провайдеров (порядок сборки)

| Priority | Provider | Источник | Когда меняется |
|---|---|---|---|
| 0 | `StaticPromptProvider("identity")` | код | при обновлении версии |
| 10 | `StaticPromptProvider("security")` | код | при обновлении версии |
| 20 | `StaticPromptProvider("tool_rules")` | код | при обновлении версии |
| 30 | `StaticPromptProvider("task_guide")` | код | при обновлении версии |
| 40 | `StaticPromptProvider("git_guide")` | код | при обновлении версии |
| 50 | `StaticPromptProvider("tone")` | код | при обновлении версии |
| 60 | `EnvironmentPromptProvider` | OS, runtime | при каждом `build()` |
| 70 | `IDEPromptProvider` | тип IDE | при старте сессии |
| 80 | `GitPromptProvider` | git CLI | при каждом `build()` |
| 90 | `FilePromptProvider("boba_md")` | `BOBA.md` | при изменении файла |
| 100 | `FilePromptProvider("memory")` | `MEMORY.md` | при изменении файла |
| 110 | `SkillsPromptProvider` | реестр skills | при регистрации |
| 120 | `DeferredToolsPromptProvider` | deferred tools | при подключении MCP |

### SystemPromptResult

```python
class SystemPromptResult:
    def __init__(self, blocks: Iterator[SystemPromptBlock]) -> None:
        self._blocks = blocks

    def build(self) -> str:
        """Конкатенация всех непустых блоков."""
        return "\n\n".join(b.content for b in self._blocks if b.content)

    def __iter__(self) -> Iterator[SystemPromptBlock]:
        return iter(self._blocks)
```

### SystemPromptService

Не привязан к workspace — workspace-зависимость определяется конкретными провайдерами.

```python
class SystemPromptService:
    async def register(provider: SystemPromptProvider) -> None
        """Зарегистрировать провайдер, вызвать provider.enter()."""

    async def unregister(id: ProviderId) -> None
        """Вызвать provider.close() и убрать провайдер."""

    def providers() -> Iterator[SystemPromptProvider]

    async def build() -> SystemPromptResult
        """Собрать system prompt из всех провайдеров (по priority)."""

    async def close() -> None
        """Вызвать close() у всех провайдеров."""
```

---

## 4. Tools

### Модели

```python
# --- Параметры инструмента ---

@dataclass(frozen=True)
class ToolParams:
    """Базовый класс. Каждый инструмент наследует свой."""
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


# --- Типизированная схема параметров ---

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


# --- Определение инструмента ---

@dataclass(frozen=True)
class ToolDefinition:
    """Что видит LLM. Передаётся в параметр tools API."""
    name: str
    description: str
    input_schema: ToolInputSchema


# --- Результат выполнения ---

@dataclass(frozen=True)
class ToolResult:
    content: str
    is_error: bool = False
```

### Tool (abstract)

```python
TParams = TypeVar("TParams", bound=ToolParams)

class Tool(ABC, Generic[TParams]):
    @property
    @abstractmethod
    def definition(self) -> ToolDefinition: ...

    @property
    @abstractmethod
    def params_type(self) -> type[TParams]: ...

    @abstractmethod
    async def execute(self, params: TParams) -> ToolResult: ...

    async def enter(self) -> None:
        pass

    async def close(self) -> None:
        pass
```

### ToolsService

Не привязан к workspace — workspace-зависимость определяется конкретными `Tool`'ами.

```python
class ToolsService:
    async def register(tool: Tool) -> None
        """Зарегистрировать инструмент, вызвать tool.enter()."""

    async def unregister(name: str) -> None
        """Вызвать tool.close() и убрать инструмент."""

    def list() -> Iterator[Tool]

    def get_definitions() -> Iterator[ToolDefinition]
        """Определения для параметра tools API."""

    async def execute(name: str, raw_args: dict[str, Any]) -> ToolResult
        """Найти tool, сконструировать params из raw JSON, выполнить."""

    async def close() -> None
        """Вызвать close() у всех инструментов."""
```

---

## 5. User Prompt

### Модели

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

### UserPromptService

```python
class UserPromptService:
    def register(template: UserPromptTemplate) -> None
    def unregister(name: str) -> None
    def list() -> Iterator[UserPromptTemplate]
    def enrich_message(user_text: str) -> str
        """Обернуть сообщение юзера шаблонами (BEFORE/AFTER)."""
```

---

## Куда что идёт в API

| Модель | Параметр API | Формат |
|---|---|---|
| `SystemPromptBlock` | `system` / `messages[role=system]` | текст (конкатенация блоков) |
| `ToolDefinition` | `tools` | `ToolInputSchema` (типизированная схема) |
| `UserPromptTemplate` | `messages[role=user]` | обёртка вокруг user message |

---

## Интеграция с DI (dishka)

### Скоупы

| Scope | Что живёт | Lifetime |
|---|---|---|
| `Scope.APP` | `WorkspaceRegistry` | singleton, весь процесс |
| `Scope.REQUEST` | Все workspace-сервисы | один запрос / сессия |

`WorkspaceId` передаётся через `from_context` при входе в REQUEST scope.

### Провайдеры

```python
# APP scope
class WorkspaceProvider(Provider):
    scope = Scope.APP

    @provide
    def registry(self) -> WorkspaceRegistry:
        return FsWorkspaceRegistry()


# REQUEST scope
class WorkspaceServicesProvider(Provider):
    scope = Scope.REQUEST

    workspace_id = from_context(provides=WorkspaceId, scope=Scope.REQUEST)

    @provide
    async def chat_history(self, ws: WorkspaceId) -> AsyncIterator[ChatHistoryService]:
        svc = FsChatHistoryService(ws)
        await svc.enter()
        yield svc
        await svc.close()

    @provide
    async def chat_config(self, ws: WorkspaceId) -> AsyncIterator[ChatConfigService]:
        svc = FsChatConfigService(ws)
        await svc.enter()
        yield svc
        await svc.close()

    @provide
    async def system_prompt_service(self, ws: WorkspaceId) -> AsyncIterator[SystemPromptService]:
        svc = SystemPromptService()

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
        await svc.close()

    @provide
    async def tools_service(self, ws: WorkspaceId) -> AsyncIterator[ToolsService]:
        svc = ToolsService()
        # await svc.register(ReadFileTool(...))
        # await svc.register(SearchDocumentsTool(...))
        yield svc
        await svc.close()

    @provide
    async def user_prompt_service(self, ws: WorkspaceId) -> AsyncIterator[UserPromptService]:
        svc = UserPromptService()
        yield svc
```

---

## Модель взаимодействия

### Обработка запроса пользователя

```
UI/API Request (workspace_uuid, user_message)
  │
  ▼
workspace = registry.get(workspace_uuid)
  │
  ▼
async with container(context={WorkspaceId: workspace}) as scope:
    │
    ├── config  = await scope.get(ChatConfigService)
    ├── history = await scope.get(ChatHistoryService)
    ├── sps     = await scope.get(SystemPromptService)
    ├── tools   = await scope.get(ToolsService)
    ├── ups     = await scope.get(UserPromptService)
    │
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
  for tool_call in assistant_response.tool_calls:
      result = await tools.execute(tool_call.name, tool_call.arguments)
    │
    ▼
  history.add_message(UserMessage(content=enriched_msg))
  history.add_message(AssistantMessage(content=..., tool_calls=[...]))

# scope закрылся → close() → ref count == 0
```

### Создание workspace

```
workspace = registry.create()  → WorkspaceId

async with container(context={WorkspaceId: workspace}) as scope:
    config = await scope.get(ChatConfigService)
    config.set("model", default_model)
```

### Удаление workspace

```
await registry.delete(workspace)
  ├── workspace.active == True  → WorkspaceBusyError
  └── workspace.active == False → ok
```

---

## Схема зависимостей

```
┌─ Scope.APP ──────────────────────────────────────────────────┐
│  WorkspaceRegistry (singleton)                               │
│    │  create() / get(uuid) / delete()                        │
│    ▼                                                         │
│  WorkspaceId (UUID + ref count)                              │
└──────────────────────────────────────────────────────────────┘
        │
        │  from_context(provides=WorkspaceId)
        ▼
┌─ Scope.REQUEST ──────────────────────────────────────────────┐
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
│  │    build() → SystemPromptResult   ├── Static              │
│  │                                   ├── File (→ WorkspaceId)│
│  │                                   ├── Environment         │
│  │                                   ├── Git                 │
│  │                                   ├── IDE                 │
│  │                                   ├── Skills              │
│  │                                   └── DeferredTools       │
│  │                                                           │
│  ├── ToolsService              ◄── Tool[TParams]             │
│  │    get_definitions()             ├── ToolDefinition       │
│  │    execute(name, args)           ├── ToolParams (иерархия)│
│  │    → ToolResult                  └── ToolResult           │
│  │                                                           │
│  └── UserPromptService         ◄── UserPromptTemplate        │
│       enrich_message(text) → str                             │
│                                                              │
└──────────────────────────────────────────────────────────────┘
```

---

## Принципы

1. **WorkspaceId — единственная точка связи.** Сервисы не знают друг о друге.
2. **Ref count — автоматический через dishka scope.** `enter()` при создании, `close()` при закрытии scope.
3. **Registry — владелец.** Удаление безопасно: только когда `active == False`.
4. **Сервисы создаются независимо.** Добавление нового — новый `@provide`.
5. **Workspace-зависимость на уровне компонентов, не сервисов.** `FilePromptProvider` и конкретные `Tool`'ы сами держат ref count через `enter()`/`close()`.

---

## Статус

- [ ] Реализовать `WorkspaceId` (UUID + ref count)
- [ ] Определить `WorkspaceRegistry`, `WorkspaceAwareService`
- [ ] Определить модели: `LLMMessage` (иерархия), `ToolCall`, `ChatConfig`
- [ ] Определить `ChatHistoryService`, `ChatConfigService`
- [ ] Определить `SystemPromptProvider` (abstract + реализации)
- [ ] Определить `SystemPromptService`, `SystemPromptResult`
- [ ] Определить модели инструментов: `ToolParams`, `ToolInputSchema`, `ToolDefinition`, `ToolResult`, `Tool`
- [ ] Определить `ToolsService`
- [ ] Определить `UserPromptTemplate`, `UserPromptService`
- [ ] Настроить dishka-провайдеры (bootstrapping)
- [ ] Реализовать Fs-имплементации
