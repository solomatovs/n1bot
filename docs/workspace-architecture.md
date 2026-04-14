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
# Иерархия immutable-сообщений (frozen=True), моделирующая роли участников
# диалога с LLM. Соответствует ролям в API OpenAI/Anthropic.
# Immutability упрощает хранение истории и исключает случайные мутации.

@dataclass(frozen=True)
class LLMMessage:
    """Базовый класс сообщения в диалоге. Не содержит content —
    каждый дочерний класс определяет его самостоятельно."""
    pass

@dataclass(frozen=True)
class SystemMessage(LLMMessage):
    """Роль 'system'. Системный промпт: инструкции и контекст для модели,
    невидимые пользователю. Задаёт поведение, ограничения и роль ассистента."""
    content: str

@dataclass(frozen=True)
class DeveloperMessage(LLMMessage):
    """Роль 'developer'. Инструкции разработчика приложения для reasoning-моделей
    (OpenAI o1/o3/o4). Модель доверяет developer-сообщениям больше, чем user.
    Для провайдеров без этой роли адаптер сериализует как system."""
    content: str

@dataclass(frozen=True)
class UserMessage(LLMMessage):
    """Роль 'user'. Сообщение от пользователя (вопрос, команда, ввод)."""
    content: str

@dataclass(frozen=True)
class AssistantMessage(LLMMessage):
    """Роль 'assistant'. Текстовый ответ модели."""
    content: str

@dataclass(frozen=True)
class AssistantToolMessage(LLMMessage):
    """Роль 'assistant'. Ответ модели с вызовами инструментов.
    content опционален — модель может сопроводить tool call'ы текстом."""
    tool_calls: list[ToolCall]

@dataclass(frozen=True)
class ToolMessage(LLMMessage):
    """Роль 'tool'. Результат выполнения инструмента.
    tool_call_id связывает результат с конкретным ToolCall.id,
    чтобы модель знала, на какой именно вызов пришёл ответ."""
    content: str
    tool_call_id: str


# --- Thinking (extended thinking, Claude API) ---

@dataclass(frozen=True)
class ThinkingMessage(LLMMessage):
    """Внутренние рассуждения модели (type: 'thinking').
    Видимы клиенту, могут отображаться пользователю."""
    content: str

@dataclass(frozen=True)
class RedactedThinkingMessage(LLMMessage):
    """Скрытые рассуждения модели (type: 'redacted_thinking').
    data — opaque payload, передаётся обратно в API для multi-turn continuity."""
    data: str


# --- Server tools (Claude API) ---
# Серверные инструменты выполняются на стороне Anthropic.
# Клиент получает вызов и результат в одном ответе, не участвует в выполнении.
# Используют те же AssistantToolMessage/ToolMessage, что и клиентские tools.
# Адаптер парсит server_tool_use в ToolCall, *_tool_result в ToolMessage.
# Серверные ToolParams определены в секции Tools.
```

### ToolCall

`ToolCall` — конкретный вызов инструмента от LLM. Живёт в `AssistantMessage.tool_calls`.
`ToolParams` (определён в секции Tools) — базовый класс аргументов.

```python
TParams = TypeVar("TParams", bound=ToolParams)

@dataclass(frozen=True)
class ToolCall(Generic[TParams]):
    id: str            # уникальный id вызова (назначается LLM)
    tool_id: ToolId    # идентификатор инструмента
    arguments: TParams # типизированные аргументы (см. секцию Tools)
```

**Пояснение `tool_call_id`:** LLM может вызвать несколько tools параллельно. `ToolMessage.tool_call_id` связывает результат с конкретным `ToolCall.id`.

### Хранение сообщений

`LLMMessage` — чистая доменная модель (содержимое). Метаданные хранения —
в обёртке `StoredMessage`. Метаданные API-ответа — в `LLMResponseMeta`.

#### MessageId

UUID, назначается при сохранении. `LLMMessage` не содержит `MessageId`.

#### StoredMessage

```python
@dataclass
class StoredMessage:
    """Сообщение + метаданные хранения."""
    id: MessageId                       # уникальный id записи
    message: LLMMessage                 # доменная модель (содержимое)
    timestamp: datetime                 # когда создано
    turn_id: str                        # группировка блоков одного логического действия
                                        # (API-ответ: thinking+text+tool_use;
                                        #  API-запрос: user message + tool_result'ы)
    parent_id: MessageId | None = None  # ссылка на предыдущую запись (цепочка)
```

#### LLMResponseMeta

```python
@dataclass(frozen=True)
class TokenUsage:
    """Статистика токенов одного API-вызова."""
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0

@dataclass(frozen=True)
class LLMResponseMeta:
    """Метаданные одного API-ответа. Один на turn_id.
    Не является частью LLMMessage — это свойство ответа целиком."""
    request_id: str                     # requestId от API
    model: str                          # какая модель ответила
    stop_reason: str                    # "tool_use" | "end_turn"
    usage: TokenUsage                   # статистика токенов
```

**Где что живёт:**

| Данные | Где | Почему |
|---|---|---|
| content, tool_calls, tool_call_id | `LLMMessage` | Содержимое — нужно для API |
| id, timestamp, parent_id, turn_id | `StoredMessage` | Метаданные хранения — не нужны для API |
| model, stop_reason, usage | `LLMResponseMeta` | Свойство ответа целиком, не отдельного сообщения |

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
    def add_message(message: LLMMessage, turn_id: str,
                    parent_id: MessageId | None = None) -> StoredMessage
    def get_messages() -> Iterator[StoredMessage]
    def get_message(message_id: MessageId) -> StoredMessage
    def get_turn(turn_id: str) -> list[StoredMessage]
        """Все сообщения одного turn'а."""
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

### ToolId

```python
class ToolId:
    """Идентификатор инструмента."""
    def __init__(self, name: str) -> None:
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    def __eq__(self, other: object) -> bool:
        return isinstance(other, ToolId) and self._name == other._name

    def __hash__(self) -> int:
        return hash(self._name)

    def __repr__(self) -> str:
        return f"ToolId({self._name!r})"
```

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
    """Чтение по строкам."""
    file_path: str
    offset: int | None = None   # номер строки (0-based)
    limit: int | None = None    # количество строк

@dataclass(frozen=True)
class ReadBytesParams(ToolParams):
    """Чтение по байтам."""
    file_path: str
    offset: int | None = None   # байтовое смещение
    limit: int | None = None    # количество байт

@dataclass(frozen=True)
class SearchParams(ToolParams):
    query: str
    top_k: int = 5

@dataclass(frozen=True)
class WriteParams(ToolParams):
    file_path: str
    content: str

@dataclass(frozen=True)
class BashParams(ToolParams):
    command: str
    timeout: int = 120000


# --- Серверные инструменты (Claude API) ---
# Используют те же ToolParams/ToolCall/AssistantToolMessage/ToolMessage.
# Адаптер Claude парсит server_tool_use → ToolCall, *_tool_result → ToolMessage.

@dataclass(frozen=True)
class WebSearchParams(ToolParams):
    """Серверный веб-поиск."""
    query: str

@dataclass(frozen=True)
class WebFetchParams(ToolParams):
    """Серверная загрузка веб-страницы."""
    url: str

@dataclass(frozen=True)
class CodeExecutionParams(ToolParams):
    """Серверное выполнение Python-кода."""
    code: str

@dataclass(frozen=True)
class BashExecutionParams(ToolParams):
    """Серверное выполнение bash-команды."""
    command: str

@dataclass(frozen=True)
class TextEditorParams(ToolParams):
    """Серверное редактирование файла."""
    command: str       # "create", "str_replace" и др.
    path: str
    file_text: str = ""
    old_str: str = ""
    new_str: str = ""


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
    id: ToolId
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

### Конкретные реализации

```python
class ReadTool(Tool[ReadParams]):
    """Построчное чтение файла. offset/limit — в строках."""

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            id=ToolId("read"),
            description="Read a file by lines.",
            input_schema=ToolInputSchema(params=[
                ParamSchema(name="file_path", type=JsonType.STRING,
                            description="Absolute path to the file."),
                ParamSchema(name="offset", type=JsonType.INTEGER,
                            description="Line number to start from (0-based).",
                            required=False, default=None),
                ParamSchema(name="limit", type=JsonType.INTEGER,
                            description="Number of lines to read.",
                            required=False, default=None),
            ]),
        )

    @property
    def params_type(self) -> type[ReadParams]:
        return ReadParams

    async def execute(self, params: ReadParams) -> ToolResult:
        path = Path(params.file_path)
        if not path.exists():
            return ToolResult(content=f"File not found: {path}", is_error=True)

        start = params.offset or 0
        limit = params.limit

        result_lines: list[str] = []
        with open(path, "r") as f:
            for _ in range(start):
                if f.readline() == "":
                    break

            line_no = start
            while True:
                line = f.readline()
                if line == "":
                    break
                line_no += 1
                result_lines.append(f"{line_no}\t{line}")
                if limit is not None and len(result_lines) >= limit:
                    break

        return ToolResult(content="".join(result_lines))


class ReadBytesTool(Tool[ReadBytesParams]):
    """Чтение файла по байтовому смещению. offset/limit — в байтах.
    Использует seek() — мгновенный переход без сканирования."""

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            id=ToolId("read_bytes"),
            description="Read a file by byte offset.",
            input_schema=ToolInputSchema(params=[
                ParamSchema(name="file_path", type=JsonType.STRING,
                            description="Absolute path to the file."),
                ParamSchema(name="offset", type=JsonType.INTEGER,
                            description="Byte offset to start from.",
                            required=False, default=None),
                ParamSchema(name="limit", type=JsonType.INTEGER,
                            description="Number of bytes to read.",
                            required=False, default=None),
            ]),
        )

    @property
    def params_type(self) -> type[ReadBytesParams]:
        return ReadBytesParams

    async def execute(self, params: ReadBytesParams) -> ToolResult:
        path = Path(params.file_path)
        if not path.exists():
            return ToolResult(content=f"File not found: {path}", is_error=True)

        with open(path, "r") as f:
            if params.offset:
                f.seek(params.offset)
            chunk = f.read(params.limit) if params.limit else f.read()

        return ToolResult(content=chunk)


class WriteTool(Tool[WriteParams]):
    """Создаёт или полностью перезаписывает файл."""

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            id=ToolId("write"),
            description="Create or overwrite a file.",
            input_schema=ToolInputSchema(params=[
                ParamSchema(name="file_path", type=JsonType.STRING,
                            description="Absolute path to the file."),
                ParamSchema(name="content", type=JsonType.STRING,
                            description="Content to write."),
            ]),
        )

    @property
    def params_type(self) -> type[WriteParams]:
        return WriteParams

    async def execute(self, params: WriteParams) -> ToolResult:
        path = Path(params.file_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(params.content)
        return ToolResult(content=f"Written {len(params.content)} bytes to {path}")
```

### ToolsService

Не привязан к workspace.

```python
class ToolsService:
    async def register(tool: Tool) -> None
        """Зарегистрировать инструмент, вызвать tool.enter()."""

    async def unregister(id: ToolId) -> None
        """Вызвать tool.close() и убрать инструмент."""

    def get_definitions() -> Iterator[ToolDefinition]
        """Определения для параметра tools API."""

    async def execute(id: ToolId, raw_args: dict[str, Any]) -> ToolResult
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

## 6. LLM Adapter

Прослойка между доменными моделями и API провайдеров.
Абстрактный интерфейс + конкретная реализация для OpenAI-совместимого API (LiteLLM).

### Стриминговая модель

Всё построено на итераторах — данные обрабатываются сообщение за сообщением,
ничего не накапливается в памяти целиком.

```python
@dataclass(frozen=True)
class CompletionDelta:
    """Один чанк стриминг-ответа LLM. Приходит от API по мере генерации."""
    content: str | None = None
    reasoning_content: str | None = None
    tool_call_index: int | None = None
    tool_call_id: str | None = None
    tool_call_name: str | None = None
    tool_call_arguments: str | None = None
    finish_reason: str | None = None    # "stop", "tool_calls" — только в последнем чанке
    request_id: str | None = None       # id API-запроса
    model: str | None = None            # модель
```

### Абстрактный интерфейс

```python
TApiMessage = TypeVar("TApiMessage")  # формат сообщения провайдера

class MessageConverter(ABC, Generic[TApiMessage]):
    """Конвертирует сообщения между доменными моделями и форматом API провайдера.
    Оба направления. Работает с Iterator — не загружает все сообщения в память."""

    @abstractmethod
    def serialize(self, messages: Iterator[LLMMessage]) -> Iterator[TApiMessage]:
        """domain → API. Может склеивать соседние сообщения
        (AssistantMessage + AssistantToolMessage → одно API-сообщение)."""
        ...

    @abstractmethod
    def deserialize(self, messages: Iterator[TApiMessage]) -> Iterator[LLMMessage]:
        """API → domain. Может разбивать одно API-сообщение
        на несколько доменных (assistant с content + tool_calls → два объекта)."""
        ...


class LLMCompletionService(ABC, Generic[TApiMessage]):
    """Стриминговый LLM completion. Возвращает Iterator[CompletionDelta].
    Потребитель итерирует дельты по мере поступления от API."""

    @abstractmethod
    def stream_completion(
        self,
        messages: Iterator[TApiMessage],
        tools: Iterator[ToolDefinition],
        model: str,
    ) -> Iterator[CompletionDelta]: ...


class DeltaAssembler(ABC):
    """Собирает поток CompletionDelta в поток готовых LLMMessage.
    Отдаёт каждое сообщение как только оно полностью собрано,
    не дожидаясь конца всего ответа."""

    @abstractmethod
    def assemble(self, deltas: Iterator[CompletionDelta]) -> Iterator[LLMMessage]: ...

    @abstractmethod
    def get_meta(self) -> LLMResponseMeta:
        """Метаданные ответа. Доступны после завершения итерации."""
        ...
```

### Поток данных

```
          converter.serialize      stream_completion          assemble
Iterator     ────────────►   Iterator      ────────────►  Iterator    ────────►  Iterator
[LLMMessage]                 [TApiMessage]                 [CompletionDelta]      [LLMMessage]

          converter.deserialize
Iterator     ◄────────────   Iterator
[LLMMessage]                 [TApiMessage]

Доменные                     API-формат                    Чанки от API           Готовые доменные
сообщения                    провайдера                    (по мере генерации)    сообщения
(история)                                                                        (по мере сборки)
```

Ничего не копится — каждый шаг отдаёт данные по мере готовности.

### Модели API (OpenAI-совместимый формат)

```python
@dataclass(frozen=True)
class ApiFunctionCall:
    """Вложенный объект function внутри tool_call."""
    name: str
    arguments: str   # JSON-строка

@dataclass(frozen=True)
class ApiToolCall:
    """Tool call в формате OpenAI API."""
    id: str
    type: str        # "function"
    function: ApiFunctionCall

@dataclass(frozen=True)
class ApiMessage:
    """Сообщение в формате OpenAI API."""
    role: str                                  # "system", "developer", "user", "assistant", "tool"
    content: str | None = None
    tool_calls: list[ApiToolCall] | None = None # только для assistant
    tool_call_id: str | None = None             # только для tool
```

### OpenAIMessageConverter

```python
class OpenAIMessageConverter(MessageConverter[ApiMessage]):
    """Конвертирует между доменными моделями и форматом OpenAI API."""

    def __init__(self, tools_service: ToolsService) -> None:
        self._tools = tools_service

    def serialize(self, messages: Iterator[LLMMessage]) -> Iterator[ApiMessage]:
        pending_assistant: AssistantMessage | None = None

        for msg in messages:
            if pending_assistant is not None:
                if isinstance(msg, AssistantToolMessage):
                    # Склейка: content + tool_calls в одно API-сообщение
                    yield ApiMessage(
                        role="assistant",
                        content=pending_assistant.content,
                        tool_calls=[self._to_api_tool_call(tc)
                                    for tc in msg.tool_calls],
                    )
                    pending_assistant = None
                    continue
                else:
                    yield self._to_api_message(pending_assistant)
                    pending_assistant = None

            if isinstance(msg, AssistantMessage):
                pending_assistant = msg
            else:
                yield self._to_api_message(msg)

        if pending_assistant is not None:
            yield self._to_api_message(pending_assistant)

    def _to_api_message(self, message: LLMMessage) -> ApiMessage:
        """Конвертация одного доменного сообщения (без склейки)."""
        match message:
            case SystemMessage(content=c):
                return ApiMessage(role="system", content=c)
            case DeveloperMessage(content=c):
                return ApiMessage(role="developer", content=c)
            case UserMessage(content=c):
                return ApiMessage(role="user", content=c)
            case AssistantMessage(content=c):
                return ApiMessage(role="assistant", content=c)
            case AssistantToolMessage(tool_calls=calls):
                return ApiMessage(
                    role="assistant",
                    tool_calls=[self._to_api_tool_call(tc) for tc in calls],
                )
            case ToolMessage(content=c, tool_call_id=tid):
                return ApiMessage(role="tool", content=c, tool_call_id=tid)

    def _to_api_tool_call(self, tc: ToolCall) -> ApiToolCall:
        return ApiToolCall(
            id=tc.id,
            type="function",
            function=ApiFunctionCall(
                name=tc.tool_id.name,
                arguments=json.dumps(asdict(tc.arguments)),
            ),
        )

    # --- API → domain ---

    def deserialize(self, messages: Iterator[ApiMessage]) -> Iterator[LLMMessage]:
        for api_msg in messages:
            match api_msg.role:
                case "system":
                    yield SystemMessage(content=api_msg.content or "")
                case "developer":
                    yield DeveloperMessage(content=api_msg.content or "")
                case "user":
                    yield UserMessage(content=api_msg.content or "")
                case "tool":
                    yield ToolMessage(
                        content=api_msg.content or "",
                        tool_call_id=api_msg.tool_call_id or "",
                    )
                case "assistant":
                    yield from self._deserialize_assistant(api_msg)

    def _deserialize_assistant(self, api_msg: ApiMessage) -> Iterator[LLMMessage]:
        if api_msg.content:
            yield AssistantMessage(content=api_msg.content)
        if api_msg.tool_calls:
            tool_calls = [self._from_api_tool_call(tc) for tc in api_msg.tool_calls]
            yield AssistantToolMessage(tool_calls=tool_calls)

    def _from_api_tool_call(self, api_tc: ApiToolCall) -> ToolCall:
        tool_id = ToolId(api_tc.function.name)
        raw_args = json.loads(api_tc.function.arguments)
        tool = self._tools.get(tool_id)
        params = tool.params_type(**raw_args)
        return ToolCall(id=api_tc.id, tool_id=tool_id, arguments=params)
```

### OpenAICompletionService

```python
class OpenAICompletionService(LLMCompletionService[ApiMessage]):
    """Стриминг через OpenAI-совместимый API."""

    def __init__(self, client: OpenAI) -> None:
        self._client = client

    def stream_completion(
        self,
        messages: Iterator[ApiMessage],
        tools: Iterator[ToolDefinition],
        model: str,
    ) -> Iterator[CompletionDelta]:
        stream = self._client.chat.completions.create(
            model=model,
            messages=list(messages),
            tools=list(tools),
            stream=True,
            stream_options={"include_usage": True},
        )

        for chunk in stream:
            if not chunk.choices:
                continue
            choice = chunk.choices[0]
            delta = choice.delta

            if delta.content:
                yield CompletionDelta(content=delta.content)

            reasoning = getattr(delta, "reasoning_content", None)
            if reasoning:
                yield CompletionDelta(reasoning_content=reasoning)

            if delta.tool_calls:
                for tc in delta.tool_calls:
                    func = tc.function
                    yield CompletionDelta(
                        tool_call_index=tc.index,
                        tool_call_id=tc.id or None,
                        tool_call_name=func.name if func else None,
                        tool_call_arguments=func.arguments if func else None,
                    )

            if choice.finish_reason:
                yield CompletionDelta(
                    finish_reason=choice.finish_reason,
                    request_id=chunk.id,
                    model=chunk.model,
                )
```

### OpenAIDeltaAssembler

```python
class OpenAIDeltaAssembler(DeltaAssembler):
    """Собирает поток CompletionDelta в поток LLMMessage.
    Yield'ит каждое сообщение как только оно готово."""

    def __init__(self, tools_service: ToolsService) -> None:
        self._tools = tools_service
        self._meta: LLMResponseMeta | None = None

    def assemble(self, deltas: Iterator[CompletionDelta]) -> Iterator[LLMMessage]:
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        # tool_calls: {index → (id, name, [argument_chunks])}
        tool_calls: dict[int, tuple[str, str, list[str]]] = {}
        finish_reason: str | None = None
        request_id: str = ""
        model: str = ""

        for delta in deltas:
            if delta.content:
                content_parts.append(delta.content)

            if delta.reasoning_content:
                reasoning_parts.append(delta.reasoning_content)

            if delta.tool_call_index is not None:
                idx = delta.tool_call_index
                if idx not in tool_calls:
                    tool_calls[idx] = (delta.tool_call_id or "", delta.tool_call_name or "", [])
                if delta.tool_call_arguments:
                    tool_calls[idx][2].append(delta.tool_call_arguments)

            if delta.finish_reason:
                finish_reason = delta.finish_reason
            if delta.request_id:
                request_id = delta.request_id
            if delta.model:
                model = delta.model

        # --- Yield готовых сообщений ---

        if reasoning_parts:
            yield ThinkingMessage(content="".join(reasoning_parts))

        if content_parts:
            yield AssistantMessage(content="".join(content_parts))

        if tool_calls:
            calls = []
            for idx in sorted(tool_calls):
                tc_id, tc_name, arg_chunks = tool_calls[idx]
                raw_args = json.loads("".join(arg_chunks))
                tool = self._tools.get(ToolId(tc_name))
                params = tool.params_type(**raw_args)
                calls.append(ToolCall(id=tc_id, tool_id=ToolId(tc_name), arguments=params))
            yield AssistantToolMessage(tool_calls=calls)

        self._meta = LLMResponseMeta(
            request_id=request_id,
            model=model,
            stop_reason=finish_reason or "",
            usage=TokenUsage(),  # usage приходит отдельно, можно расширить
        )

    def get_meta(self) -> LLMResponseMeta:
        if self._meta is None:
            raise RuntimeError("assemble() not consumed yet")
        return self._meta
```

### Пример использования

```python
# --- DI создаёт конкретные реализации, код работает через абстракции ---
converter: MessageConverter = OpenAIMessageConverter(tools_service)
completion: LLMCompletionService = OpenAICompletionService(client)
assembler: DeltaAssembler = OpenAIDeltaAssembler(tools_service)

# --- Agent loop ---

while True:
    # 1. Сериализация: Iterator[LLMMessage] → Iterator[ApiMessage]
    api_messages = converter.serialize(history.get_messages())

    # 2. Стриминг: Iterator[ApiMessage] → Iterator[CompletionDelta]
    deltas = completion.stream_completion(api_messages, tools, model)

    # 3. Сборка: Iterator[CompletionDelta] → Iterator[LLMMessage]
    turn_id = uuid4().hex
    for msg in assembler.assemble(deltas):
        # Каждое сообщение сохраняется сразу, не копится
        history.add_message(msg, turn_id=turn_id, parent_id=last_id)

    # 4. Проверка: продолжать или выйти
    meta = assembler.get_meta()
    if meta.stop_reason in ("stop", "end_turn"):
        break

    # 5. Выполнение tools → ToolMessage → следующая итерация цикла
    for msg in history.get_turn(turn_id):
        if isinstance(msg.message, AssistantToolMessage):
            for tc in msg.message.tool_calls:
                result = await tools_service.execute(tc.tool_id, tc.arguments)
                history.add_message(
                    ToolMessage(content=result.content, tool_call_id=tc.id),
                    turn_id=turn_id, parent_id=last_id,
                )
```

### ToolsService: новый метод get()

```python
class ToolsService:
    # ... существующие методы ...

    def get(self, id: ToolId) -> Tool:
        """Найти инструмент по ToolId. Raises ToolNotFoundError."""
```

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
