"""События агента (``AgentEvent``) — канал наблюдений **только в sink'и**
(UI, телеметрия, журнал). Event'ы эмитит источник через ``yield`` в
:class:`StreamSource`; потребляют sink'и через ``match-case``. Каждое
событие — immutable (``frozen=True`` dataclass), несёт стабильное имя
через classmethod :meth:`BaseEvent.name` (``Literal[...]``-типизировано
для exhaustive проверки декодерами).

**Важно — разделение ответственности:**

- Events ↔ sink'и (пользователь / UI / телеметрия). Чистый one-way
  поток наблюдений.
- LLM ↔ :class:`MessageService`. Для feedback-loop'а (вернуть LLM
  результат tool'а, ошибку формата, нотис юзера) middleware/router
  пишут ``LLMMessage`` в :class:`MessageService` — LLM на следующей
  итерации читает историю при сборке запроса. **LLM events не
  видит.**

Исключения из «events — для sink'ов» — это middleware, которые
наблюдают свой upstream (например, :class:`AssistantMessagePersistence`
агрегирует токены, :class:`RepeatedFormatFailureGuardMiddleware`
считает сбои формата). Они не «потребители» в смысле sink-ов —
внутренние state-трекеры, которые прокачивают events дальше.
Specification'ы типа :class:`StopOnAnyFailure` смотрят events для
control-flow (остановить цикл) — тоже системная роль, не sink.

Event'ы классифицированы по **семействам** (orthogonal от concrete-типа):
sink-автор подписывается на семью и ловит всю категорию без enumeration
concrete'ов. Это основной механизм расширяемости — добавление нового
concrete'а в семью **не требует правок** sink'ов, подписанных на семью.


════════════════════════════════════════════════════════════════════
  Полная иерархия
════════════════════════════════════════════════════════════════════

::

    BaseEvent (abstract, frozen dataclass)
    │   request_id: RequestId
    │   + classmethod name() -> Literal["..."]
    │
    ├── LifecycleMarker (abstract)        границы фазы, без контента
    │   ├── IterationStarted                iteration, max_iterations
    │   ├── StageStarted / StageCompleted   stage [+ detail]
    │   ├── SystemPromptProcessingStarted   content_before
    │   ├── SystemPromptProcessed           + content_after, duration_ms
    │   ├── UserPromptProcessingStarted     content_before
    │   ├── UserPromptProcessed             + content_after, duration_ms
    │   ├── LLMRequestSent                  model, messages_count, has_tools
    │   ├── GenerationStarted
    │   ├── ThinkingStarted
    │   ├── AnswerStarted
    │   ├── ToolCallBegin                   index, tool_call_id, tool_name
    │   ├── ToolExecutionStarted            tool_call_id, tool_name
    │   └── GenerationDone                  finish_reason
    │
    ├── StreamingDelta (abstract)         инкрементальные куски контента
    │   ├── ThinkingToken                   token
    │   ├── AnswerToken                     token
    │   ├── RefusalToken                    token
    │   └── ToolCallArgumentDelta           index, arguments
    │
    ├── DurableMessage (abstract)         завершённые message-единицы
    │   ├── UserQueryReceived               query
    │   ├── ThinkingComplete                content (агрегированный reasoning)
    │   ├── AnswerComplete                  content
    │   ├── AnswerDiscarded                 (корректирующее — отменяет
    │   │                                    ранее добавленное сообщение)
    │   ├── RefusalComplete                 content
    │   ├── ToolCallComplete                tool_call_id, tool_name, arguments
    │   └── ToolResultReady                 tool_call_id, tool_name, content
    │
    ├── UserNotification (abstract)       уведомление → sink (user-facing),
    │   │   message: str                    цикл продолжается. Events — это
    │   │                                   sink-канал; LLM-feedback идёт
    │   │                                   через MessageService, не через
    │   │                                   events, поэтому все «цикл идёт»
    │   │                                   нотисы — одна семья.
    │   ├── UserNoticeReady                 severity: info|warning|error
    │   │                                   (общие нотисы — deprecation,
    │   │                                   soft-reject валидации, fallback)
    │   ├── ToolExecutionFailed             error_kind, tool_call_id, tool_name
    │   │                                   (tool упал — observability)
    │   └── ToolCallFormatFailed            error_kind
    │                                       (LLM сломала формат tool call —
    │                                       observability; критика для LLM
    │                                       пишется параллельно в
    │                                       MessageService middleware'ом)
    │
    └── TerminalFailure (abstract)        ошибка, цикл останавливается
        │   error_kind: str                 (через StopOnAnyFailure)
        │   message: str
        ├── GenerationFailed                retryable, status_code
        ├── PromptFailed                    retryable, provider
        ├── PersistenceFailed               retryable
        ├── MaxIterationsReached            limit, iteration
        └── RepeatedFormatFailure           count, limit

**UserNotification vs TerminalFailure** — обе user-facing, разница в
контракте с циклом:

- ``UserNotification`` — цикл продолжается, sink показывает нотис.
- ``TerminalFailure`` — :class:`StopOnAnyFailure` остановит цикл,
  sink показывает финальную ошибку.

Sink'и, которым нужна «любая проблема» — объединяют:
``case TerminalFailure() | UserNotification() as n: ...``.


════════════════════════════════════════════════════════════════════
  Семейство → use-case (sink authoring guide)
════════════════════════════════════════════════════════════════════

┌─────────────────────┬───────────────────────────────────────────────┐
│ Семейство           │ Типичный sink                                 │
├─────────────────────┼───────────────────────────────────────────────┤
│ LifecycleMarker     │ TelemetrySink — waterfall, durations          │
│ StreamingDelta      │ UI — inline rendering токенов                 │
│ DurableMessage      │ HistorySink / AuditSink / TranscriptLogger    │
│ UserNotification    │ UI notification area, AlertSink               │
│ TerminalFailure     │ UI error-banner, Sentry, AlertSink            │
└─────────────────────┴───────────────────────────────────────────────┘


════════════════════════════════════════════════════════════════════
  Примеры
════════════════════════════════════════════════════════════════════

**Subscription на семью** (``HistorySink``)::

    def handle(self, ctx, event):
        match event:
            case DurableMessage():
                self._append_to_transcript(event)
            case _:
                pass

**Subscription на concrete** (UI — разное рендеринг per-тип)::

    match event:
        case AnswerToken(token=t):
            sys.stdout.write(t)
        case ThinkingToken(token=t):
            sys.stdout.write(dim(t))
        case ToolResultReady(tool_name=fn, content=c):
            print(f"[{fn}] {c[:200]}")

**Общий handler на любую проблему** (семьи объединяются ``|``)::

    case TerminalFailure() as f:
        sentry.capture(f.error_kind, f.message)
    case UserNotification() as n:
        ui.show_notification(n.message)


════════════════════════════════════════════════════════════════════
  Как добавить новый event
════════════════════════════════════════════════════════════════════

1. Выбери семейство:

   - граница фазы                 → :class:`LifecycleMarker`
   - кусок потока                 → :class:`StreamingDelta`
   - завершённое сообщение        → :class:`DurableMessage`
   - нотис пользователю (любой)   → :class:`UserNotification`
   - ошибка, цикл стоп            → :class:`TerminalFailure`
   - ничего не подходит           → наследуй от :class:`BaseEvent`
     напрямую (пока не появится второй похожий — тогда выделим семью).

2. Добавь ``@dataclass(frozen=True)`` с полями.
3. Реализуй ``classmethod name() -> Literal["..."]``.
4. Занеси в :data:`AgentEvent`-union и :data:`AgentEventName`-literal
   (синхронность проверит :func:`_verify_event_names_exhaustive`).
5. Sink'и, подписанные на семью, получат новый event автоматически.
   Per-concrete sink'и обновлять не надо, если им не нужен особый
   рендеринг.


════════════════════════════════════════════════════════════════════
  Что НЕ должно быть AgentEvent
════════════════════════════════════════════════════════════════════

Внутренние состояния middleware, буферы, счётчики — это implementation
details, а не наблюдения. Event — стабильный контракт между источником
и sink'ами, сохраняемый в журнал и пересылаемый по сети. Если данные
нужны только одному middleware — держи их в ``ctx`` / локальной
переменной, не эмить событие.

Event не несёт референс на mutable-объект (``frozen=True`` dataclass
ловит статически). Для mutable-состояния нужен отдельный механизм
(:class:`~boba.domain.agent.messages.MessageService`,
:class:`~boba.domain.agent.models.AgentContext`).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal, TypeAlias, assert_never

from boba.domain.agent.models import RequestId
from boba.domain.core.errors import UserNoticeSeverity
from boba.domain.core.patterns import Converter, Serializer


class FinishReason(StrEnum):
    """Причина завершения генерации LLM.

    Нормализованные значения ``finish_reason`` из OpenAI-совместимых API.
    Наследование от :class:`StrEnum` даёт прозрачную JSON-сериализацию
    (значение записывается как строка) и совместимость с ``str`` при
    сравнениях.
    """

    STOP = "stop"
    LENGTH = "length"
    TOOL_CALLS = "tool_calls"
    CONTENT_FILTER = "content_filter"

    @property
    def is_terminal(self) -> bool:
        """Останавливает ли это завершение цикл агента.

        ``TOOL_CALLS`` — единственное не-терминальное значение: LLM просит
        выполнить tool, цикл делает следующую итерацию с результатом.
        Остальные варианты означают, что LLM закончила работу над запросом
        (успешно, обрывом по длине или фильтром контента) — продолжать
        бессмысленно.

        Реализовано через исчерпывающий ``match`` с :func:`assert_never`
        в хвосте: при добавлении нового значения в enum pyright укажет на
        непокрытую ветку, заставив явно классифицировать — а не молча
        унаследовать поведение одного из лагерей.
        """
        match self:
            case FinishReason.TOOL_CALLS:
                return False
            case FinishReason.STOP | FinishReason.LENGTH | FinishReason.CONTENT_FILTER:
                return True
            case _:
                assert_never(self)


@dataclass(frozen=True)
class BaseEvent(ABC):
    """Базовый класс для всех событий агента."""

    request_id: RequestId

    @classmethod
    @abstractmethod
    def name(cls) -> str:
        """Стабильное имя типа события для сериализации.

        Каждый подкласс **должен** возвращать свою ``Literal["..."]``-строку
        (не просто ``str``): это позволяет статическому анализатору проверять
        исчерпывающее покрытие match-case в декодерах.
        """
        ...


# ═════════════════════════════════════════════════════════════════════
#  Семейства событий (orthogonal классификация для sink'ов)
# ═════════════════════════════════════════════════════════════════════
#
# Concrete-события наследуются от одного семейства. Sink-автор
# match'ит по семейству и ловит **всю** категорию разом — без
# enumeration 20+ concrete-типов. Новый concrete в семью —
# автоматически попадает в подписанные sink'и.


@dataclass(frozen=True)
class LifecycleMarker(BaseEvent, ABC):
    """Граница фазы (старт / конец стадии, без контента).

    Для телеметрии и waterfall-трейсинга. Не несёт пользовательских
    данных сообщения, только отметки времени. Может иметь
    ``duration_ms`` в concrete-классах, маркирующих завершение.
    """


@dataclass(frozen=True)
class StreamingDelta(BaseEvent, ABC):
    """Инкрементальный кусок контента между ``*Started`` и ``*Complete``.

    Concrete-события держат свой field под чанк (``token`` /
    ``arguments``) — у sink'ов, которые буферизуют агрегированно, есть
    единая точка подписки; сама структура chunk-а специфична.
    """


@dataclass(frozen=True)
class DurableMessage(BaseEvent, ABC):
    """Готовое сообщение, идёт в историю/транскрипт.

    HistorySink / AuditSink подписывается на это семейство вместо
    перечисления concrete-типов. :class:`AnswerDiscarded` тоже здесь —
    корректирующее событие, отменяющее ранее добавленное сообщение.
    """


@dataclass(frozen=True)
class UserNotification(BaseEvent, ABC):
    """Уведомление пользователю через sink, цикл агента продолжается.

    Events — канал только в sink'и (user-facing observability). LLM
    уведомления через события НЕ получает; её feedback-loop
    обслуживает :class:`MessageService` (middleware/router пишут в неё
    ``LLMMessage``). Поэтому все нотисы «цикл идёт» — одна семья,
    независимо от того, произошла ли попутно ошибка tool'а или это
    простой deprecation-warning.

    ``message`` — текст для рендеринга. Concrete-children добавляют
    свои поля (``severity`` для стилизации, ``error_kind`` /
    ``tool_call_id`` / ... для категоризации).

    TerminalFailure — отдельная семья: там другой контракт (цикл
    останавливается через :class:`StopOnAnyFailure`).
    """

    message: str


@dataclass(frozen=True)
class UserQueryReceived(DurableMessage):
    """Запрос пользователя принят."""

    query: str

    @classmethod
    def name(cls) -> Literal["UserQueryReceived"]:
        return "UserQueryReceived"


@dataclass(frozen=True)
class IterationStarted(LifecycleMarker):
    """Начало новой итерации агентского цикла.

    Эмитится :class:`IterationCounterMiddleware` после инкремента
    ``ctx.iteration`` и до проверки лимита. Sink'и UI используют для
    отрисовки границ итераций — особенно полезно при длинных цепочках
    tool calls, когда иначе не видно, сколько из ``max_iterations``
    уже потрачено.
    """

    iteration: int
    max_iterations: int

    @classmethod
    def name(cls) -> Literal["IterationStarted"]:
        return "IterationStarted"


@dataclass(frozen=True)
class StageStarted(LifecycleMarker):
    stage: str

    @classmethod
    def name(cls) -> Literal["StageStarted"]:
        return "StageStarted"


@dataclass(frozen=True)
class StageCompleted(LifecycleMarker):
    stage: str
    detail: str

    @classmethod
    def name(cls) -> Literal["StageCompleted"]:
        return "StageCompleted"


@dataclass(frozen=True)
class SystemPromptProcessingStarted(LifecycleMarker):
    """System-prompt middleware начинает сборку system-prompt.

    Эмитится до вызова :class:`PromptFactory`. ``content_before`` —
    состояние ``ctx.llm_request.system_prompt`` до middleware (обычно
    ``None`` на первой итерации, но на последующих может содержать
    прежний system-prompt).
    """

    content_before: str | None

    @classmethod
    def name(cls) -> Literal["SystemPromptProcessingStarted"]:
        return "SystemPromptProcessingStarted"


@dataclass(frozen=True)
class SystemPromptProcessed(LifecycleMarker):
    """System-prompt middleware завершил сборку system-prompt.

    ``duration_ms`` — время работы :class:`PromptFactory`.
    ``content_before`` / ``content_after`` — состояние слота до/после.
    """

    content_before: str | None
    content_after: str | None
    duration_ms: float

    @classmethod
    def name(cls) -> Literal["SystemPromptProcessed"]:
        return "SystemPromptProcessed"


@dataclass(frozen=True)
class UserPromptProcessingStarted(LifecycleMarker):
    """User-prompt middleware начинает сборку user-prompt.

    Эмитится до вызова :class:`PromptFactory`. ``content_before`` —
    состояние ``ctx.llm_request.user_prompt`` до middleware (обычно
    ``None`` на первой итерации): даёт sink'ам возможность показать,
    с чего началось преобразование.
    """

    content_before: str | None

    @classmethod
    def name(cls) -> Literal["UserPromptProcessingStarted"]:
        return "UserPromptProcessingStarted"


@dataclass(frozen=True)
class UserPromptProcessed(LifecycleMarker):
    """User-prompt middleware завершил сборку user-prompt.

    ``duration_ms`` — время работы :class:`PromptFactory` (и записи
    результата в ``ctx.llm_request.user_prompt``). ``content_before`` и
    ``content_after`` — состояние слота до/после, для диффа в UI/журнале.
    """

    content_before: str | None
    content_after: str | None
    duration_ms: float

    @classmethod
    def name(cls) -> Literal["UserPromptProcessed"]:
        return "UserPromptProcessed"


@dataclass(frozen=True)
class LLMRequestSent(LifecycleMarker):
    """HTTP-запрос к LLM-провайдеру отправлен, стрим-handle получен.

    Эмитится адаптером сразу после возврата ``chat.completions.create``,
    до итерации по чанкам. Закрывает TTFT-слепое пятно между «агент
    решил идти в LLM» и ``GenerationStarted`` (первый чанк). Для UX
    нужен, когда cold-start модели / очередь прокси отдают первый
    токен через 1–20 секунд.
    """

    model: str
    messages_count: int
    has_tools: bool

    @classmethod
    def name(cls) -> Literal["LLMRequestSent"]:
        return "LLMRequestSent"


@dataclass(frozen=True)
class GenerationStarted(LifecycleMarker):
    """Первый chunk от LLM — генерация началась."""

    @classmethod
    def name(cls) -> Literal["GenerationStarted"]:
        return "GenerationStarted"


@dataclass(frozen=True)
class ThinkingStarted(LifecycleMarker):
    """Модель начала thinking/reasoning."""

    @classmethod
    def name(cls) -> Literal["ThinkingStarted"]:
        return "ThinkingStarted"


@dataclass(frozen=True)
class ThinkingToken(StreamingDelta):
    """Chunk thinking/reasoning от LLM."""

    token: str

    @classmethod
    def name(cls) -> Literal["ThinkingToken"]:
        return "ThinkingToken"


@dataclass(frozen=True)
class AnswerStarted(LifecycleMarker):
    """Модель начала генерировать ответ."""

    @classmethod
    def name(cls) -> Literal["AnswerStarted"]:
        return "AnswerStarted"


@dataclass(frozen=True)
class AnswerToken(StreamingDelta):
    """Chunk текстового ответа от LLM."""

    token: str

    @classmethod
    def name(cls) -> Literal["AnswerToken"]:
        return "AnswerToken"


@dataclass(frozen=True)
class RefusalToken(StreamingDelta):
    """Chunk отказа модели отвечать."""

    token: str

    @classmethod
    def name(cls) -> Literal["RefusalToken"]:
        return "RefusalToken"


@dataclass(frozen=True)
class GenerationDone(LifecycleMarker):
    """Генерация завершена."""

    finish_reason: FinishReason = FinishReason.STOP

    def __post_init__(self) -> None:
        # Нормализуем строку → enum: decoder передаёт сырое значение из
        # JSON, а frozen-dataclass не даёт обычного присваивания.
        if not isinstance(self.finish_reason, FinishReason):
            object.__setattr__(
                self, "finish_reason", FinishReason(self.finish_reason)
            )

    @classmethod
    def name(cls) -> Literal["GenerationDone"]:
        return "GenerationDone"


@dataclass(frozen=True)
class TerminalFailure(BaseEvent, ABC):
    """Базовый класс терминальных отказов — гарантирует ``error_kind`` и ``message``.

    Позволяет sink'ам маршрутизировать все terminal-события в один handler
    Подклассы добавляют свои
    специфичные поля (``retryable``, ``limit``, ``status_code``, …).
    """

    error_kind: str
    message: str


@dataclass(frozen=True)
class GenerationFailed(TerminalFailure):
    """Терминальный отказ: адаптер/retry не смогли получить ответ LLM."""

    retryable: bool
    status_code: int | None = None

    @classmethod
    def name(cls) -> Literal["GenerationFailed"]:
        return "GenerationFailed"


@dataclass(frozen=True)
class PromptFailed(TerminalFailure):
    """Терминальный отказ: PromptFactory/провайдер не смогли собрать промпт."""

    retryable: bool
    provider: str | None = None

    @classmethod
    def name(cls) -> Literal["PromptFailed"]:
        return "PromptFailed"


@dataclass(frozen=True)
class PersistenceFailed(TerminalFailure):
    """Терминальный отказ: не удалось прочитать/записать журнал/хранилище."""

    retryable: bool

    @classmethod
    def name(cls) -> Literal["PersistenceFailed"]:
        return "PersistenceFailed"


@dataclass(frozen=True)
class MaxIterationsReached(TerminalFailure):
    """Терминальный отказ: агентский цикл исчерпал лимит итераций."""

    limit: int
    iteration: int

    @classmethod
    def name(cls) -> Literal["MaxIterationsReached"]:
        return "MaxIterationsReached"


@dataclass(frozen=True)
class RepeatedFormatFailure(TerminalFailure):
    """Терминальный отказ: модель залипла на неверном формате tool call."""

    count: int
    limit: int

    @classmethod
    def name(cls) -> Literal["RepeatedFormatFailure"]:
        return "RepeatedFormatFailure"


@dataclass(frozen=True)
class ToolCallBegin(LifecycleMarker):
    """Начало tool call — пришёл id и имя функции."""

    index: int
    tool_call_id: str
    tool_name: str

    @classmethod
    def name(cls) -> Literal["ToolCallBegin"]:
        return "ToolCallBegin"


@dataclass(frozen=True)
class ToolCallArgumentDelta(StreamingDelta):
    """Chunk аргументов tool call."""

    index: int
    arguments: str

    @classmethod
    def name(cls) -> Literal["ToolCallArgumentDelta"]:
        return "ToolCallArgumentDelta"


@dataclass(frozen=True)
class ToolCallComplete(DurableMessage):
    """Агрегированный tool call: имя + полные аргументы."""

    tool_call_id: str
    tool_name: str
    arguments: str

    @classmethod
    def name(cls) -> Literal["ToolCallComplete"]:
        return "ToolCallComplete"


@dataclass(frozen=True)
class ToolExecutionStarted(LifecycleMarker):
    """Tool начал исполняться.

    Эмитится :class:`ToolExecutionMiddleware` после разбора аргументов
    и до вызова ``tool.execute(...)``. Закрывает слепое пятно между
    ``ToolCallComplete`` (LLM дописала args) и ``ToolResultReady``
    (результат готов) — для медленных tools (fs, http) UI иначе
    выглядит зависшим. Если разбор args провалился, событие не
    эмитится — вместо него роутер шлёт :class:`ToolCallFormatFailed`
    или :class:`ToolExecutionFailed`.
    """

    tool_call_id: str
    tool_name: str

    @classmethod
    def name(cls) -> Literal["ToolExecutionStarted"]:
        return "ToolExecutionStarted"


@dataclass(frozen=True)
class ToolResultReady(DurableMessage):
    """Результат успешного выполнения tool.

    Ошибки сюда не попадают — для них есть :class:`ToolExecutionFailed`.
    """

    tool_call_id: str
    tool_name: str
    content: str

    @classmethod
    def name(cls) -> Literal["ToolResultReady"]:
        return "ToolResultReady"


@dataclass(frozen=True)
class UserNoticeReady(UserNotification):
    """Нотис пользователю с severity (info / warning / error).

    Эмитится роутером из :class:`UserNoticeError` или middleware
    напрямую. Sink'и UI отрисуют по ``severity`` — разные цвета/иконки.
    В :class:`MessageService` не попадает — LLM нотис не видит.
    Use cases: deprecation-warnings, soft-reject валидации,
    информирование о fallback'е.
    """

    severity: UserNoticeSeverity

    @classmethod
    def name(cls) -> Literal["UserNoticeReady"]:
        return "UserNoticeReady"


@dataclass(frozen=True)
class ToolExecutionFailed(UserNotification):
    """Tool упал — sink пользователя видит факт ошибки.

    Цикл продолжается. Feedback для LLM (``LLMMessage(role="tool")``)
    параллельно пишется в :class:`MessageService` middleware'ом /
    роутером — это отдельный канал, не через events. Событие нужно
    sink'ам (UI/журнал), чтобы отрисовать/залогировать факт ошибки
    отдельно от успешного результата.
    """

    error_kind: str
    tool_call_id: str
    tool_name: str

    @classmethod
    def name(cls) -> Literal["ToolExecutionFailed"]:
        return "ToolExecutionFailed"


@dataclass(frozen=True)
class ToolCallFormatFailed(UserNotification):
    """LLM нарушила формат content-as-JSON tool call'а.

    Цикл продолжается. Feedback для LLM (``LLMMessage(role="user")``)
    пишется в :class:`MessageService` роутером — LLM на следующей
    итерации увидит критику и сможет переформулировать. Событие —
    observability для sink'ов.

    Tool call не состоялся (разбор провалился до определения ``name``/
    ``tool_call_id``), поэтому поля идентификации вызова отсутствуют —
    в отличие от :class:`ToolExecutionFailed`.
    """

    error_kind: str

    @classmethod
    def name(cls) -> Literal["ToolCallFormatFailed"]:
        return "ToolCallFormatFailed"


@dataclass(frozen=True)
class ThinkingComplete(DurableMessage):
    """Агрегированный thinking: весь текст рассуждений.

    Наследуется от :class:`DurableMessage` как «завершённая message-
    единица» (в отличие от :class:`ThinkingToken`-дельт). Решение,
    персистить ли reasoning в :class:`MessageService`, принимает
    middleware — sink'и просто наблюдают; семья отражает семантику
    события, а не действия sink-а.
    """

    content: str

    @classmethod
    def name(cls) -> Literal["ThinkingComplete"]:
        return "ThinkingComplete"


@dataclass(frozen=True)
class AnswerComplete(DurableMessage):
    """Агрегированный ответ: весь текст ответа."""

    content: str

    @classmethod
    def name(cls) -> Literal["AnswerComplete"]:
        return "AnswerComplete"


@dataclass(frozen=True)
class AnswerDiscarded(DurableMessage):
    """Ранее отправленные ``AnswerToken``-ы для этого ``request_id``
    следует отбросить: аккумулированный ``content`` не должен попасть
    ни в итоговое ``AnswerComplete``, ни в ``LLMMessage.content``.

    Эмитится, когда middleware решает переинтерпретировать уже
    проэмиченный текстовый поток как tool call: токены ушли наружу
    ради отзывчивости UI, но в долговременном состоянии (сообщение в
    истории, журнал) текстовой ветки быть не должно — её заменяют
    ``ToolCallBegin`` / ``ToolCallArgumentDelta`` / ``ToolCallComplete``.

    Потребители (``AssistantMessagePersistenceMiddleware``, ``HistorySink``)
    очищают свои answer-буферы для данного ``request_id``; sink'и UI
    могут использовать событие как сигнал «стереть/перезаписать то, что
    только что вывели».
    """

    @classmethod
    def name(cls) -> Literal["AnswerDiscarded"]:
        return "AnswerDiscarded"


@dataclass(frozen=True)
class RefusalComplete(DurableMessage):
    """Агрегированный отказ: весь текст отказа."""

    content: str

    @classmethod
    def name(cls) -> Literal["RefusalComplete"]:
        return "RefusalComplete"


AgentEvent = (
    UserQueryReceived
    | IterationStarted
    | StageStarted
    | StageCompleted
    | SystemPromptProcessingStarted
    | SystemPromptProcessed
    | UserPromptProcessingStarted
    | UserPromptProcessed
    | LLMRequestSent
    | GenerationStarted
    | ThinkingStarted
    | ThinkingToken
    | ThinkingComplete
    | AnswerStarted
    | AnswerToken
    | AnswerComplete
    | AnswerDiscarded
    | RefusalToken
    | RefusalComplete
    | GenerationDone
    | GenerationFailed
    | PromptFailed
    | PersistenceFailed
    | MaxIterationsReached
    | RepeatedFormatFailure
    | ToolCallBegin
    | ToolCallArgumentDelta
    | ToolCallComplete
    | ToolExecutionStarted
    | ToolResultReady
    | ToolExecutionFailed
    | ToolCallFormatFailed
    | UserNoticeReady
)


# Literal-union всех wire-имён событий. Должен содержать ровно те же строки,
# что возвращают ``name()`` подклассов ``BaseEvent``. Синхронность проверяет
# статически функция ``_verify_event_names_exhaustive`` ниже.
AgentEventName: TypeAlias = Literal[
    "UserQueryReceived",
    "IterationStarted",
    "StageStarted",
    "StageCompleted",
    "SystemPromptProcessingStarted",
    "SystemPromptProcessed",
    "UserPromptProcessingStarted",
    "UserPromptProcessed",
    "LLMRequestSent",
    "GenerationStarted",
    "ThinkingStarted",
    "ThinkingToken",
    "ThinkingComplete",
    "AnswerStarted",
    "AnswerToken",
    "AnswerComplete",
    "AnswerDiscarded",
    "RefusalToken",
    "RefusalComplete",
    "GenerationDone",
    "GenerationFailed",
    "PromptFailed",
    "PersistenceFailed",
    "MaxIterationsReached",
    "RepeatedFormatFailure",
    "ToolCallBegin",
    "ToolCallArgumentDelta",
    "ToolCallComplete",
    "ToolExecutionStarted",
    "ToolResultReady",
    "ToolExecutionFailed",
    "ToolCallFormatFailed",
    "UserNoticeReady",
]


def _verify_event_names_exhaustive(e: AgentEvent) -> AgentEventName:
    """Compile-time гарантия: union ``AgentEvent.name()`` == ``EventName``.

    Не вызывается в runtime. pyright проверит, что возвращаемое значение
    ``e.name()`` (union всех Literal-типов) присваивается в ``EventName``.

    При добавлении нового ``AgentEvent`` и забытом имени в ``EventName``
    pyright сообщит: Literal["NewEvent"] is not assignable to EventName.
    """
    return e.name()


# Типы сериализации AgentEvent
AgentEventEncoder = Converter[AgentEvent, str]
AgentEventDecoder = Converter[str, AgentEvent]
AgentEventSerializer = Serializer[AgentEvent, str]
