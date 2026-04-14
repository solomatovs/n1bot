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

### SystemPromptId

```python
class SystemPromptId:
    """Идентификатор провайдера."""
    def __init__(self, name: str) -> None:
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    def __eq__(self, other: object) -> bool:
        return isinstance(other, SystemPromptId) and self._name == other._name

    def __hash__(self) -> int:
        return hash(self._name)

    def __repr__(self) -> str:
        return f"SystemPromptId({self._name!r})"
```

### SystemPromptProvider (abstract)

```python
class SystemPromptProvider(ABC):
    @property
    @abstractmethod
    def id(self) -> SystemPromptId: ...

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

Вынесены в [system-prompt-providers.md](system-prompt-providers.md):
`StaticPromptProvider`, `FilePromptProvider`, `EnvironmentPromptProvider`,
`GitPromptProvider`, `IDEPromptProvider`, `SkillsPromptProvider`, `CallbackPromptProvider`.

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

    async def unregister(id: SystemPromptId) -> None
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

# Конкретные ToolParams вынесены в tools-implementations.md


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

Вынесены в [tools-implementations.md](tools-implementations.md):
конкретные `ToolParams` (ReadParams, WriteParams, BashParams, серверные)
и `Tool` реализации (ReadTool, ReadBytesTool, WriteTool).

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

class MessageSerializer(ABC, Generic[TApiMessage]):
    """Сериализует доменные модели в формат API провайдера.
    Работает с Iterator — не загружает все сообщения в память."""

    @abstractmethod
    def serialize(self, messages: Iterator[LLMMessage]) -> Iterator[TApiMessage]:
        """domain → API. Может склеивать соседние сообщения
        (AssistantMessage + AssistantToolMessage → одно API-сообщение)."""
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
         serializer.serialize     stream_completion          assemble
Iterator     ────────────►   Iterator      ────────────►  Iterator    ────────►  Iterator
[LLMMessage]                 [TApiMessage]                 [CompletionDelta]      [LLMMessage]

Доменные                     API-формат                    Чанки от API           Готовые доменные
сообщения                    провайдера                    (по мере генерации)    сообщения
(история)                                                                        (по мере сборки)
```

Ничего не копится — каждый шаг отдаёт данные по мере готовности.

### Конкретные реализации

Вынесены в [openai-adapter.md](openai-adapter.md):
API модели (ApiMessage, ApiToolCall, ApiFunctionCall),
OpenAIMessageSerializer, OpenAICompletionService, OpenAIDeltaAssembler,
пример agent loop.

---

## 7. Pipeline & Streaming

Базовые абстракции для потоковой обработки и композиции стадий.

### PipelineStage

```python
TContext = TypeVar("TContext")
TEvent = TypeVar("TEvent")

class PipelineStage(ABC, Generic[TContext, TEvent]):
    """Одна стадия пайплайна.
    Читает данные из ctx, записывает результаты,
    yield'ит события для наблюдаемости."""

    @property
    @abstractmethod
    def name(self) -> str: ...

    @abstractmethod
    def run(self, ctx: TContext) -> Iterator[TEvent]: ...
```

### Pipeline

```python
class Pipeline(Generic[TContext, TEvent]):
    """Выполняет последовательность стадий PipelineStage.
    Оркестратор — генератор, ничего не накапливает."""

    def __init__(self, stages: list[PipelineStage[TContext, TEvent]]) -> None:
        self._stages = list(stages)

    @property
    def stage_names(self) -> list[str]:
        return [s.name for s in self._stages]

    def run(self, ctx: TContext) -> Iterator[TEvent]:
        """Выполнить все стадии, yield'я события по мере появления."""
        for stage in self._stages:
            yield from stage.run(ctx)
```

### StreamTransformer

```python
class StreamTransformer(ABC, Generic[TIn, TOut]):
    """Stateful поэлементная трансформация потока.
    feed() принимает один элемент, yield'ит ноль или более результатов."""

    @abstractmethod
    def feed(self, item: TIn) -> Iterator[TOut]: ...

    def flush(self) -> Iterator[TOut]:
        """Финализация — yield остатков буфера."""
        yield from ()

    @abstractmethod
    def reset(self) -> None: ...
```

### StreamConsumer

```python
class StreamConsumer(ABC, Generic[TStream, TEvent]):
    """Потребитель потока — yield'ит события, накапливает результат.
    После завершения consume() результат доступен через атрибуты."""

    @abstractmethod
    def consume(self, stream: TStream) -> Iterator[TEvent]: ...
```

---

## 8. AgentLoop

Оркестратор агентного цикла: подготовка контекста через Pipeline,
затем цикл LLM → tools → repeat. Всё на Iterator'ах.
Интегрирует модули: ChatHistoryService, SystemPromptService, UserPromptService,
MessageSerializer, LLMCompletionService, DeltaAssembler, ToolsService.

### AgentRequest / AgentConfig

```python
@dataclass(frozen=True)
class AgentRequest:
    """Входные данные для AgentLoop.run()."""
    query: str
    model: str
    max_tokens: int

@dataclass(frozen=True)
class AgentConfig:
    """Настройки AgentLoop."""
    max_iterations: int = 10
    default_model: str = ""
    limit_message: str = "Достигнут лимит итераций агента."
```

### AgentContext

```python
@dataclass
class AgentContext:
    """Контекст, передаваемый через Pipeline и AgentLoop.
    Мутабельный — стадии и цикл дополняют его."""
    request: AgentRequest
    history: ChatHistoryService
    tools: ToolsService
    turn_id: str                          # текущий turn
    last_message_id: MessageId | None = None
```

### События (AgentEvent)

```python
# Общие
@dataclass(frozen=True)
class StageStarted:
    stage: str

@dataclass(frozen=True)
class StageCompleted:
    stage: str
    detail: str

# Генерация (стриминг)
@dataclass(frozen=True)
class ThinkingToken:
    token: str

@dataclass(frozen=True)
class AnswerToken:
    token: str

@dataclass(frozen=True)
class GenerationDone:
    pass

# Tool calls
@dataclass(frozen=True)
class ToolCallStarted:
    tool_call_id: str
    tool_name: str
    arguments: str

@dataclass(frozen=True)
class ToolResultReady:
    tool_call_id: str
    tool_name: str
    content: str
    is_error: bool = False

AgentEvent = Union[
    StageStarted, StageCompleted,
    ThinkingToken, AnswerToken, GenerationDone,
    ToolCallStarted, ToolResultReady,
]
```

### Context Pipeline — стадии подготовки

Стадии работают с нашими сервисами через `AgentContext`.

```python
ContextPipeline = Pipeline[AgentContext, AgentEvent]


class SystemPromptStage(PipelineStage[AgentContext, AgentEvent]):
    """Собирает system prompt через SystemPromptService
    и сохраняет SystemMessage в историю."""

    def __init__(self, system_prompt_service: SystemPromptService) -> None:
        self._sps = system_prompt_service

    @property
    def name(self) -> str:
        return "system_prompt"

    def run(self, ctx: AgentContext) -> Iterator[AgentEvent]:
        yield StageStarted(stage=self.name)
        result = self._sps.build()
        content = result.build()
        stored = ctx.history.add_message(
            SystemMessage(content=content),
            turn_id=ctx.turn_id, parent_id=ctx.last_message_id,
        )
        ctx.last_message_id = stored.id
        yield StageCompleted(stage=self.name, detail=f"{len(content)} chars")


class UserPromptStage(PipelineStage[AgentContext, AgentEvent]):
    """Обогащает запрос пользователя через UserPromptService
    и сохраняет UserMessage в историю."""

    def __init__(self, user_prompt_service: UserPromptService) -> None:
        self._ups = user_prompt_service

    @property
    def name(self) -> str:
        return "user_prompt"

    def run(self, ctx: AgentContext) -> Iterator[AgentEvent]:
        yield StageStarted(stage=self.name)
        enriched = self._ups.enrich_message(ctx.request.query)
        stored = ctx.history.add_message(
            UserMessage(content=enriched),
            turn_id=ctx.turn_id, parent_id=ctx.last_message_id,
        )
        ctx.last_message_id = stored.id
        yield StageCompleted(stage=self.name, detail="query enriched")
```

### AgentLoop

```python
class AgentLoop:
    """Агентный цикл: ContextPipeline → (LLM → tools)* → ответ.
    Все результаты сохраняются в ChatHistoryService."""

    def __init__(
        self,
        config: AgentConfig,
        serializer: MessageSerializer,
        llm: LLMCompletionService,
        assembler: DeltaAssembler,
        context_pipeline: ContextPipeline,
        tools: ToolsService,
    ) -> None:
        self._config = config
        self._serializer = serializer
        self._llm = llm
        self._assembler = assembler
        self._pipeline = context_pipeline
        self._tools = tools

    def run(self, request: AgentRequest, ctx: AgentContext) -> Iterator[AgentEvent]:
        # 1. Подготовка контекста через Pipeline
        yield from self._pipeline.run(ctx)

        # 2. Агентный цикл
        for iteration in range(1, self._config.max_iterations + 1):
            turn_id = uuid4().hex

            # Сериализация истории → API формат
            api_messages = self._serializer.serialize(
                msg.message for msg in ctx.history.get_messages()
            )

            # Стриминг LLM
            deltas = self._llm.stream_completion(
                api_messages, self._tools.get_definitions(), request.model,
            )

            # Сборка дельт → доменные сообщения + yield событий
            for msg in self._assembler.assemble(deltas):
                stored = ctx.history.add_message(
                    msg, turn_id=turn_id, parent_id=ctx.last_message_id,
                )
                ctx.last_message_id = stored.id

                # Yield события в зависимости от типа сообщения
                match msg:
                    case AssistantMessage(content=c):
                        yield AnswerToken(token=c)
                    case ThinkingMessage(content=c):
                        yield ThinkingToken(token=c)

            meta = self._assembler.get_meta()

            # Проверка: tool calls или конец
            if meta.stop_reason in ("stop", "end_turn"):
                yield GenerationDone()
                yield StageCompleted(
                    stage="agent_loop",
                    detail=f"{iteration} итераций",
                )
                return

            # Выполнение инструментов
            for stored_msg in ctx.history.get_turn(turn_id):
                if isinstance(stored_msg.message, AssistantToolMessage):
                    for tc in stored_msg.message.tool_calls:
                        yield ToolCallStarted(
                            tool_call_id=tc.id,
                            tool_name=tc.tool_id.name,
                            arguments=str(tc.arguments),
                        )
                        result = self._tools.execute(tc.tool_id, tc.arguments)
                        tool_msg = ToolMessage(
                            content=result.content, tool_call_id=tc.id,
                        )
                        stored = ctx.history.add_message(
                            tool_msg, turn_id=turn_id,
                            parent_id=ctx.last_message_id,
                        )
                        ctx.last_message_id = stored.id
                        yield ToolResultReady(
                            tool_call_id=tc.id,
                            tool_name=tc.tool_id.name,
                            content=result.content,
                            is_error=result.is_error,
                        )

        # Лимит итераций
        yield AnswerToken(token=self._config.limit_message)
        yield GenerationDone()
        yield StageCompleted(
            stage="agent_loop",
            detail=f"лимит {self._config.max_iterations} итераций",
        )
```

### Поток выполнения

```
AgentLoop.run(request, ctx)
    │
    ▼
1. Context Pipeline (стадии работают с сервисами):
    SystemPromptStage   → SystemPromptService.build()
                          → history.add_message(SystemMessage)
    UserPromptStage     → UserPromptService.enrich_message()
                          → history.add_message(UserMessage)
    (расширяемо: SearchStage, IndexStage, ...)
    │
    ▼
2. Agent Loop (до max_iterations):
    ┌───────────────────────────────────────────────────────┐
    │  serializer.serialize(history.get_messages())         │
    │       ↓ Iterator[TApiMessage]                         │
    │  llm.stream_completion(api_messages, tools, model)    │
    │       ↓ Iterator[CompletionDelta]                     │
    │  assembler.assemble(deltas)                           │
    │       ↓ Iterator[LLMMessage]                          │
    │       → history.add_message(msg, turn_id)             │
    │       → yield AnswerToken / ThinkingToken              │
    │                                                       │
    │  meta = assembler.get_meta()                          │
    │                                                       │
    │  if stop → yield GenerationDone → return              │
    │  if tool_calls:                                       │
    │       tools.execute(tool_call)                        │
    │       → history.add_message(ToolMessage, turn_id)     │
    │       → yield ToolCallStarted, ToolResultReady        │
    │       → continue loop                                 │
    └───────────────────────────────────────────────────────┘
    │
    ▼
3. Все события yield'ятся наружу как Iterator[AgentEvent]
   (SSE стрим к клиенту)
```

### Какой сервис когда работает

| Шаг | Сервис | Что делает |
|---|---|---|
| Pipeline: system_prompt | `SystemPromptService` | Собирает промпт из провайдеров |
| Pipeline: user_prompt | `UserPromptService` | Обогащает запрос пользователя |
| Pipeline: * | `ChatHistoryService` | Сохраняет SystemMessage, UserMessage |
| Loop: сериализация | `MessageSerializer` | domain → API формат |
| Loop: стриминг | `LLMCompletionService` | API → дельты |
| Loop: сборка | `DeltaAssembler` | Дельты → доменные сообщения |
| Loop: сохранение | `ChatHistoryService` | Сохраняет AssistantMessage, AssistantToolMessage |
| Loop: tools | `ToolsService` | Выполняет инструменты |
| Loop: tool result | `ChatHistoryService` | Сохраняет ToolMessage |

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

        await svc.register(StaticPromptProvider(SystemPromptId("identity"),   0,  IDENTITY_TEXT))
        await svc.register(StaticPromptProvider(SystemPromptId("security"),   10, SECURITY_TEXT))
        await svc.register(StaticPromptProvider(SystemPromptId("tool_rules"), 20, TOOL_RULES_TEXT))
        await svc.register(StaticPromptProvider(SystemPromptId("task_guide"), 30, TASK_GUIDE_TEXT))
        await svc.register(StaticPromptProvider(SystemPromptId("git_guide"),  40, GIT_GUIDE_TEXT))
        await svc.register(StaticPromptProvider(SystemPromptId("tone"),       50, TONE_TEXT))
        await svc.register(EnvironmentPromptProvider())
        await svc.register(IDEPromptProvider(ide_type="vscode"))
        await svc.register(GitPromptProvider())
        await svc.register(FilePromptProvider(SystemPromptId("boba_md"), 90,  ws, ws_path,     "BOBA.md"))
        await svc.register(FilePromptProvider(SystemPromptId("memory"),  100, ws, memory_path, "MEMORY.md"))

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
