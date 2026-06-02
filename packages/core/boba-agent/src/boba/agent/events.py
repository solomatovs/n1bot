"""События агент-слоя.

Архитектура:

1. Базовая модель `AgentEventBase`, от которой построены 5 категорий событий.
   AgentEventBase несёт поля, общие для всех событий:

    `type`
        дискриминатор конкретного класса события.
        Используется `AgentEventAdapter` и `AgentEventRegistry` для
        (де)сериализации. Совпадает с именем класса.

    `category`
        маркер семейства (`EventCategory`)
        Sink не должен знать конкретный event,
        но он должен знать все возможные категории event'ов

    `request_id`
        идентификатор пользовательского запроса, к которому
        относится событие.
        Все события одного запроса разделяют это значение.
        на нём строится группировка, счётчики, replay на фронте

    `seq`
        монотонный счётчик per-request_id, начинается с 1.
        Стабилен в рамках одной сессии стрима.
        Клиент использует для детекта gap и реконнекта
        (пропустил seq=5 → запросил replay с 5).

    `emitted_at`
        wall-clock UTC момента, когда событие покинуло агентский стрим
        Не путать с `monotonic_ns` на отдельных событиях LLM-фазы
        те несут провайдерское время для latency-метрик.

    `iteration`
        индекс текущей итерации агентского цикла на момент эмиссии evnet.
        Стампер обновляет своё внутреннее значение по `IterationStarted.iteration_count`
        и проставляет на все последующие события того же `request_id`.
        Позволяет фронту восстановить дерево turn'а без stateful-обработки
        `IterationStarted`.

    Поля `type`, `category`, `request_id` обязан проставить производитель события.
    Поля `seq`, `emitted_at`, `iteration` заполняются автоматически в
    `EventStamperMiddleware` в самом конце middleware-цепочки
    сразу под `HistoryRecorderMiddleware`.
    поэтому журнал тоже видит уже стампленные события.


2. Существуют следующие категории событий — каждая является базой для
   конкретных реализаций и фиксирует **минимальный контракт** полей,
   достаточный для отображения / обработки без знания конкретного типа события.

   Пять «доменных» категорий (`PhaseEvent`, `DeltaEvent`,
   `MessageEvent`, `AdvisoryEvent`, `TerminalEvent`) несут информацию,
   которая является частью диалога / истории и должна быть показана
   пользователю. Шестая категория `DiagnosticEvent` несёт нефункциональную
   телеметрию (метрики, тайминги, трейсы), которая по умолчанию **скрыта**
   и существует исключительно для отладки.

    `PhaseEvent`
        граница фазы агентского цикла -
            - новая итерация
            - запрос к LLM
            - начало generation/thinking/answer
            - retry
            - завершение генерации thinking/answer
            - объявление tool call'а
            ...
        Не несёт стримящийся контент — только факт перехода + контекст.

        Поля:
            `label`     - короткий заголовок для UI
            `severity`  - info по умолчанию, warn для retry
            `details`   - key→value для отображения деталей
            `body`      - опциональное расширенное описание

    `DeltaEvent`
        инкрементальный кусок контента, который стримится в открытый «слот» UI.
        Соединяется с `MessageEvent` через общий `stream_id`:
        фронт открывает поток на первой delta и закрывает на снапшоте.

        Поля:
            `stream_id`     - идентификатор сущности
            `request_id`    - для answer/thinking/refusal
            `tool_call_id`  - для tool-вызова
            `stream_kind`   - тип потока:
                ANSWER
                THINKING
                REFUSAL
                TOOL_INVOCATION
                ...
            `chunk`         - текстовый кусок, который надо доскролировать в UI

    `MessageEvent`
        завершённое сообщение в диалоге.
        Финальная форма того, что собиралось из delta:
        - `AnswerMessage` после серии `AnswerDelta`

        либо самостоятельное снапшот-событие, у
        которого стриминга не было
            `ToolCallMessage`      - завершение вызова tool
            `UserQueryReceived`     - получение запроса от пользователя
            `FeedbackToLLMAdded`    - добавление feedback для llm

        Снапшоты пишутся в `HistoryService` через отдельный HistoryRecorderMiddleware
        Писать delta думаю нет смысла, но тоже возможно если потребуется.

        Поля:
            `stream_id`     - идентификатор сущности
            `stream_kind`   - тип потока:
                ANSWER
                THINKING
                REFUSAL
                TOOL_INVOCATION  - вызов tool (args)
                TOOL_RESULT      - результат выполнения tool
                ...
            `body`          - агрегированный контент
            `headline`      - опциональный заголовок — для tool это имя инструмента

    `AdvisoryEvent`
        нефатальное уведомление. что-то пошло не так, но цикл агента продолжает работать
            - Tool упал
            - LLM выдала невалидный JSON в args
            ...

        Sink должен показывать, не прерывая основной поток

        Поля:
            `headline`  - короткое описание для шапки
            `severity`  - warn по умолчанию
            `details`   - key→value
            `body`      - полный текст ошибки

    `TerminalEvent`
        фатальное уведомление: цикл агента остановлен.
        `StopOnAnyFailure` в `StreamSourceLoop` ловит такие события
        и завершает цикл агента.
        Sink должен паказывать их как финальное сосояние обработки запроса

        Поля:
            `headline`      - короткое описание для шапки
            `severity`      - error по умолчанию
            `details`       - key→value
            `body`          - полный текст ошибки
            `error_kind`    - имя класса исходной ошибки, для классификации в UI

    `DiagnosticEvent`
        нефункциональное уведомление - метрики, тайминги, трейсы middleware,
        результаты внутренних шагов агента и т.п. Не несёт информации,
        без которой основной поток теряет смысл - sink имеет право
        полностью игнорировать категорию, и диалог останется консистентным.

        Используется для скрытых-по-умолчанию деталей работы агента:
        - длительность выполнения tool'ов и retry-цепочка
        - usage-токены и тайминги LLM-генерации
        - аргументы tool-вызовов в развёрнутом виде
        - трейс прохождения события через middleware

        Sink фильтрует события по `topic` (dot-separated namespace,
        например "tool.timing", "llm.usage", "retry") - allow/deny-list
        задаётся пользователем через UI-toggle или CLI-флаг.

        События этой категории **не пишутся** в `HistoryService` -
        они эфемерны и существуют только в стриме.

        Поля:
            `topic`     - dot-separated namespace для фильтрации
            `headline`  - короткое описание для шапки
            `severity`  - info по умолчанию
            `details`   - key→value структурных данных
            `body`      - расширенный payload (json-dump, stacktrace и т.п.)
            `related`   - ссылки на доменные события для группировки в UI
                          (например {"tool_call_id": "..."} - фронт привязывает
                          диагностику к конкретному tool-step'у)

    Добавление нового event не требует переписывания (де)сериализализации.
    Так как все события это pydentic-модели с самоописанием.
    Все что необходимо sink уже должно лежать в событии

    Sink (потребитель) может работать реализовав только 5 категорий событий.
    Этого достаточно что бы не потерять ни одно событие.
    Однако для более детальной обработки он может реализовать обработку
        специализированных событий, например `ToolExecutionStarted` меняет индикацию
        tool-step на «выполняется…»

3. (De)сериализация — гибрид sealed-union и открытого реестра:

    - `AgentEvent` — sealed union всех core-событий.
        Используется во внутреннем коде агента (middleware, оркестратор)
        даёт compile-time проверку для match выражений
        Поэтому при добавлении нового AgentEvent тайпчекеры (mypy/pyright)
        покажут ошибку и заставят не забыть обработать это событие программиста

    - `AgentEventAdapter` — TypeAdapter поверх AgentEvent с discriminator='type'.
        Используется для (де)сериализации событий из core-набора
        например в JsonLinesHistoryService

    - `AgentEventRegistry` — открытый реестр для transport-слоя.
      Core-события регистрируются на импорте;
      внешний модуль может добавить свой класс события через `register()`.
      `decode(data)` для неизвестного `type` возвращает `UnknownAgentEvent`
      у которого сохранена `category` и доменные поля лежат в `payload`.
      Sink, диспатчащий по `category`, продолжает работать на любых неизвестных
      типах — нет «жёстких» падений на validate, ни на бэке, ни на фронте.
"""

from __future__ import annotations

import json
from abc import abstractmethod
from collections.abc import Mapping
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Any, ClassVar, Final, Literal, Self, TypeAlias

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationError,
    model_validator,
)

from boba.agent.models import (
    ToolCallFailure,
    ToolCallResult,
)
from boba.llm.events import FinishReason
from boba.llm.models import AssistantMessage, RequestId, ToolCall, ToolCallDecodeFailure
from boba.tools.domain import (
    ChartResult,
    ErrorResult,
    JsonResult,
    PgCopyTextResult,
    TableResult,
    TextResult,
)

# --------------------------------------------------------------------- #
# Enums
# --------------------------------------------------------------------- #


class Severity(StrEnum):
    """Уровень события для sink"""

    INFO = "info"
    WARN = "warn"
    ERROR = "error"


class EventCategory(StrEnum):
    """Маркер семейства события — sink диспатчит именно на это поле"""

    PHASE = "phase"
    DELTA = "delta"
    MESSAGE = "message"
    ADVISORY = "advisory"
    TERMINAL = "terminal"
    DIAGNOSTIC = "diagnostic"


class StreamKind(StrEnum):
    """Тип потока контента — канал, по которому идут delta и/или snapshot.

    Делится на два класса (см. `DeltaStreamKind`):

    - streamable — инкрементальный вывод LLM, есть и `DeltaEvent`, и
      `MessageEvent`: ANSWER, THINKING, REFUSAL, TOOL_INVOCATION;
    - snapshot-only — контент рождается целиком (от пользователя,
      tool-runner'а, агента), `DeltaEvent` нет: USER_QUERY, TOOL_RESULT,
      FEEDBACK.
    """

    # --- streamable (delta + snapshot) --------------------------------
    THINKING = "thinking"
    ANSWER = "answer"
    REFUSAL = "refusal"
    # TOOL_INVOCATION — вызов tool: args от LLM, стримятся по чанкам.
    TOOL_INVOCATION = "tool_invocation"

    # --- snapshot-only (только MessageEvent) --------------------------
    USER_QUERY = "user_query"
    FEEDBACK = "feedback"
    # TOOL_RESULT — результат выполнения tool-runner'ом, приходит целиком.
    TOOL_RESULT = "tool_result"


# Подмножество `StreamKind`, допустимое для `DeltaEvent`: только каналы,
# по которым контент стримится инкрементально. Снапшот-онли каналы
# (USER_QUERY / TOOL_RESULT / FEEDBACK) дельты не имеют — тип это запрещает.
DeltaStreamKind = Literal[
    StreamKind.THINKING,
    StreamKind.ANSWER,
    StreamKind.REFUSAL,
    StreamKind.TOOL_INVOCATION,
]


# --------------------------------------------------------------------- #
# Envelope + категории
# --------------------------------------------------------------------- #


_UNSTAMPED_SEQ: Final = -1
_UNSTAMPED_ITERATION: Final = 0


def _epoch_utc() -> datetime:
    """
    Sentinel-значение (незаполненное) `emitted_at` до стампа
    Заменяется EventStamper
    """
    return datetime.fromtimestamp(0, tz=UTC)


class AgentEventBase(BaseModel):
    """
    Базовый envelope для всех событий агента.

    `seq`, `emitted_at`, `iteration` имеют дефолты — их проставляет
    `EventStamperMiddleware` на выходе агентского стрима
    Производитель события передаёт только доменные поля + `request_id`.
    """

    model_config = ConfigDict(extra="forbid")

    # Имена производных `@property` подкласса — это вычисляемые ВЫХОДЫ, а не
    # входные поля. Старый журнал мог сериализовать их как поля; на входе
    # выкидываем, чтобы `extra="forbid"` не падал. Настоящие лишние ключи
    # при этом всё равно отлавливаются. Каждая база-категория переопределяет.
    _derived_keys: ClassVar[frozenset[str]] = frozenset()

    type: str
    category: EventCategory
    request_id: RequestId
    seq: int = _UNSTAMPED_SEQ
    emitted_at: datetime = Field(default_factory=_epoch_utc)
    iteration: int = _UNSTAMPED_ITERATION

    @model_validator(mode="before")
    @classmethod
    def _drop_derived_keys(cls, data: Any) -> Any:
        if isinstance(data, dict) and cls._derived_keys:
            return {k: v for k, v in data.items() if k not in cls._derived_keys}
        return data


class PhaseEvent(AgentEventBase):
    """
    Граница фазы в round-trip (туда-обратно)
    """

    category: Literal[EventCategory.PHASE] = EventCategory.PHASE

    _derived_keys: ClassVar[frozenset[str]] = frozenset({"label", "details"})

    severity: Severity = Severity.INFO
    body: str | None = None
    # Наносекунды с момента предыдущего PhaseEvent того же request_id.
    # Стампится `EventStamperMiddleware` через `time.monotonic_ns()`.
    # Для первого PhaseEvent в request — 0.
    duration_ns: int = 0

    @property
    @abstractmethod
    def label(self) -> str:
        """Короткий заголовок фазы для UI."""

    @property
    @abstractmethod
    def details(self) -> Mapping[str, str]:
        """key→value детали фазы для отображения."""


class DeltaEvent(AgentEventBase):
    """
    Инкрементальный кусок в поток — `stream_id` + `stream_kind` + `chunk`
    """

    category: Literal[EventCategory.DELTA] = EventCategory.DELTA

    _derived_keys: ClassVar[frozenset[str]] = frozenset({"stream_id", "chunk"})

    stream_kind: DeltaStreamKind = StreamKind.ANSWER

    @property
    @abstractmethod
    def stream_id(self) -> str:
        """
        stream_id — это идентификатор «слота» в UI,
        к которому привязывается стримящийся контент.
        Его задача — склеить два потока событий в одну визуальную сущность:

        - DeltaEvent (инкрементальные чанки) открывают слот и дописывают в него текст
        - MessageEvent (снапшот) с тем же stream_id закрывает слот готовым агрегатом

        Таким образом event'ы связаны в ui по stream_id, а не по type
        """

    @property
    @abstractmethod
    def chunk(self) -> str:
        """Текстовый инкремент для дозаписи в UI."""


class MessageEvent(AgentEventBase):
    """
    Завершённое сообщение — `stream_id` + `stream_kind` + `body`
    """

    category: Literal[EventCategory.MESSAGE] = EventCategory.MESSAGE

    _derived_keys: ClassVar[frozenset[str]] = frozenset(
        {"stream_id", "body", "headline"}
    )

    stream_kind: StreamKind = StreamKind.ANSWER

    @property
    @abstractmethod
    def stream_id(self) -> str:
        """Идентификатор сущности (request_id / tool_call_id)."""

    @property
    @abstractmethod
    def body(self) -> str:
        """Агрегированный контент сообщения."""

    @property
    def headline(self) -> str | None:
        """Опциональный заголовок — для tool это имя инструмента, иначе None."""
        return None


class AdvisoryEvent(AgentEventBase):
    """
    Нефатальное уведомление (но некая ошибка)
    агентский цикл бдет продолжен

    `status_code` и `cause_chain` авто-проставляются `AgentErrorRouter`'ом
    из исключения; концы цепочки видны в `body` через `compose_error_body`.
    """

    category: Literal[EventCategory.ADVISORY] = EventCategory.ADVISORY

    _derived_keys: ClassVar[frozenset[str]] = frozenset(
        {"headline", "details", "body"}
    )

    severity: Severity = Severity.WARN
    status_code: int | None = None
    cause_chain: tuple[str, ...] = ()

    @property
    @abstractmethod
    def headline(self) -> str:
        """Короткое описание ошибки для шапки."""

    @property
    @abstractmethod
    def details(self) -> Mapping[str, str]:
        """key→value детали ошибки."""

    @property
    @abstractmethod
    def body(self) -> str | None:
        """Полный текст ошибки."""


class TerminalEvent(AgentEventBase):
    """
    Фатальное уведомление
    агентский цикл будет остановлен

    `status_code` и `cause_chain` авто-проставляются `AgentErrorRouter`'ом
    из исключения; концы цепочки видны в `body` через `compose_error_body`.
    """

    category: Literal[EventCategory.TERMINAL] = EventCategory.TERMINAL

    _derived_keys: ClassVar[frozenset[str]] = frozenset(
        {"headline", "details", "body"}
    )

    severity: Severity = Severity.ERROR
    error_kind: str = ""
    status_code: int | None = None
    cause_chain: tuple[str, ...] = ()

    @property
    @abstractmethod
    def headline(self) -> str:
        """Короткое описание ошибки для шапки."""

    @property
    @abstractmethod
    def details(self) -> Mapping[str, str]:
        """key→value детали ошибки."""

    @property
    @abstractmethod
    def body(self) -> str | None:
        """Полный текст ошибки."""


class DiagnosticEvent(AgentEventBase):
    """
    Нефункциональное событие - метрики, тайминги, трейсы.

    Существует исключительно для отладки и наблюдаемости. Sink имеет право
    полностью игнорировать категорию: основной диалоговый поток останется
    консистентным и без этих событий. По умолчанию скрыто в UI; включается
    пользователем через фильтр по `topic`.

    Не пишется в `HistoryService` - события эфемерны и живут только в стриме.

    Producer'ы (middleware, tool-runner, llm-слой) эмитят `DiagnosticEvent`
    рядом со своими доменными событиями; связь с доменным событием передаётся
    через `related` (например, `tool_call_id` для привязки к tool-step'у).
    """

    category: Literal[EventCategory.DIAGNOSTIC] = EventCategory.DIAGNOSTIC

    topic: str = ""
    headline: str = ""
    severity: Severity = Severity.INFO
    details: Mapping[str, str] = Field(default_factory=dict)
    body: str | None = None
    related: Mapping[str, str] = Field(default_factory=dict)


def _error_details(error_kind: str, status_code: int | None) -> dict[str, str]:
    """Стандартный `details` для error-событий: kind + status_code если есть."""
    out: dict[str, str] = {"kind": error_kind}
    if status_code is not None:
        out["status_code"] = str(status_code)
    return out


def _severity_for_finish_reason(reason: FinishReason) -> Severity:
    """Визуальная приоритизация finish_reason для PhaseEvent.severity."""
    match reason:
        case FinishReason.LENGTH:
            return Severity.WARN
        case FinishReason.CONTENT_FILTER:
            return Severity.ERROR
        case FinishReason.STOP | FinishReason.TOOL_CALLS:
            return Severity.INFO


def compose_error_body(
    primary: str,
    status_code: int | None,
    cause_chain: tuple[str, ...],
) -> str:
    """Стандартный body для error-событий: основное сообщение + status + цепочка.

    Любое event-конкретное `_derive` собирает свой `primary` (message,
    args+error, raw+error), а добавление `status` и `Caused by:` —
    единое для всех ошибок.
    """
    lines: list[str] = [primary] if primary else []
    if status_code is not None:
        lines.append(f"status: {status_code}")
    if cause_chain:
        if lines:
            lines.append("")
        lines.append("Caused by:")
        lines.extend(f"  {item}" for item in cause_chain)
    return "\n".join(lines)


# --------------------------------------------------------------------- #
# PhaseEvent — конкретные события
# --------------------------------------------------------------------- #


class IterationStarted(PhaseEvent):
    """
    Начало новой итерации агентского цикла
    """

    type: Literal["IterationStarted"] = "IterationStarted"
    iteration_count: int
    max_iterations: int

    @property
    def label(self) -> str:
        return f"iteration {self.iteration_count}/{self.max_iterations}"

    @property
    def details(self) -> Mapping[str, str]:
        return {
            "iteration": str(self.iteration_count),
            "max": str(self.max_iterations),
        }


class ToolExecutionStarted(PhaseEvent):
    """Tool готов к исполнению — несёт полный `ToolCall` (id, name, args).

    UI рендерит жизненный цикл tool-step'а именно с этого события:
    создаётся UI-блок с args; финализируется по `ToolResultReady`
    или `ToolExecutionFailed` с тем же `call.id`. `ToolCallMessage`
    (намерение LLM) в UI не отображается.
    """

    type: Literal["ToolExecutionStarted"] = "ToolExecutionStarted"
    call: ToolCall

    @property
    def label(self) -> str:
        return f"tool exec: {self.call.name}"

    @property
    def details(self) -> Mapping[str, str]:
        return {"id": self.call.id, "name": self.call.name}


class TotalMessage(PhaseEvent):
    """
    Граница «генерация LLM завершена»: исход (`finish_reason`) + канонический
    `AssistantMessage` для истории.

    Это `PhaseEvent`, а НЕ content-snapshot: контент уже отрисован per-field
    `*Message` событиями (`AnswerMessage`, `ThinkingMessage`, `ToolCallMessage`),
    а здесь — агрегат + исход. Sink не должен рисовать его как реплику диалога
    (иначе дубль); он маркирует конец round-trip и подсвечивает аномалии исхода.

    Эмитится последним — после всех per-field `*Message`/`ToolCallDecodeFailedMessage`
    событий; источник истины для реконструкции истории. Несёт всё, что пришло от
    провайдера за одну генерацию:

    - `message` — собранный AssistantMessage с плоскими полями
      (content / thinking / refusal / tool_calls / tool_call_decode_failures).
      Пустой message — валидное состояние, когда модель завершилась без контента.
    - `finish_reason` — то, что реально прислал провайдер; без подмен.

    Единственное событие, на котором принимается решение об остановке
    агентского цикла — см. `StopIfNotToolCall`, `StopIfLengthReached`,
    `StopIfContentFilter`.
    """

    type: Literal["TotalMessage"] = "TotalMessage"
    message: AssistantMessage
    finish_reason: FinishReason

    @property
    def label(self) -> str:
        n = len(self.message.tool_calls)
        return f"generation completed ({self.finish_reason.value}, tool_calls={n})"

    @property
    def details(self) -> Mapping[str, str]:
        return {
            "finish_reason": self.finish_reason.value,
            "tool_calls": str(len(self.message.tool_calls)),
            "has_answer": str(bool(self.message.content)),
            "has_thinking": str(bool(self.message.thinking)),
            "has_refusal": str(bool(self.message.refusal)),
        }

    @model_validator(mode="after")
    def _set_severity(self) -> Self:
        # severity — настоящее (сериализуемое) поле, но его значение выводится
        # из finish_reason; это не плоская проекция, поэтому остаётся полем.
        # LENGTH — ответ обрезан (warn); CONTENT_FILTER — заблокирован (error);
        # STOP / TOOL_CALLS — обычный финал (info).
        self.severity = _severity_for_finish_reason(self.finish_reason)
        return self


# --------------------------------------------------------------------- #
# DeltaEvent — конкретные события
# --------------------------------------------------------------------- #


class ThinkingDelta(DeltaEvent):
    """
    Chunk reasoning-токена
    """

    type: Literal["ThinkingDelta"] = "ThinkingDelta"
    stream_kind: Literal[StreamKind.THINKING] = StreamKind.THINKING
    token: str

    @property
    def stream_id(self) -> str:
        return str(self.request_id)

    @property
    def chunk(self) -> str:
        return self.token


class AnswerDelta(DeltaEvent):
    """
    Chunk текстового ответа для отображения пользователю
    """

    type: Literal["AnswerDelta"] = "AnswerDelta"
    stream_kind: Literal[StreamKind.ANSWER] = StreamKind.ANSWER
    token: str

    @property
    def stream_id(self) -> str:
        return str(self.request_id)

    @property
    def chunk(self) -> str:
        return self.token


class RefusalDelta(DeltaEvent):
    """
    Chunk отказа модели отвечать
    """

    type: Literal["RefusalDelta"] = "RefusalDelta"
    stream_kind: Literal[StreamKind.REFUSAL] = StreamKind.REFUSAL
    token: str

    @property
    def stream_id(self) -> str:
        return str(self.request_id)

    @property
    def chunk(self) -> str:
        return self.token


class ToolCallDelta(DeltaEvent):
    """
    Chunk аргументов tool call
    tool call тоже приходит частами
    """

    type: Literal["ToolCallDelta"] = "ToolCallDelta"
    stream_kind: Literal[StreamKind.TOOL_INVOCATION] = StreamKind.TOOL_INVOCATION
    index: int
    tool_call_id: str
    tool_name: str
    arguments_chunk: str

    @property
    def stream_id(self) -> str:
        return self.tool_call_id

    @property
    def chunk(self) -> str:
        return self.arguments_chunk


# --------------------------------------------------------------------- #
# MessageEvent — конкретные события
# --------------------------------------------------------------------- #


class UserQueryReceived(MessageEvent):
    """Запрос пользователя принят агентом и записан в историю."""

    type: Literal["UserQueryReceived"] = "UserQueryReceived"
    stream_kind: Literal[StreamKind.USER_QUERY] = StreamKind.USER_QUERY
    query: str

    @property
    def stream_id(self) -> str:
        return str(self.request_id)

    @property
    def body(self) -> str:
        return self.query


class ThinkingMessage(MessageEvent):
    """Агрегированный reasoning итерации."""

    type: Literal["ThinkingMessage"] = "ThinkingMessage"
    stream_kind: Literal[StreamKind.THINKING] = StreamKind.THINKING
    content: str

    @property
    def stream_id(self) -> str:
        return str(self.request_id)

    @property
    def body(self) -> str:
        return self.content


class AnswerMessage(MessageEvent):
    """Агрегированный текстовый ответ итерации"""

    type: Literal["AnswerMessage"] = "AnswerMessage"
    stream_kind: Literal[StreamKind.ANSWER] = StreamKind.ANSWER
    content: str

    @property
    def stream_id(self) -> str:
        return str(self.request_id)

    @property
    def body(self) -> str:
        return self.content


class RefusalMessage(MessageEvent):
    """Агрегированный отказ модели."""

    type: Literal["RefusalMessage"] = "RefusalMessage"
    stream_kind: Literal[StreamKind.REFUSAL] = StreamKind.REFUSAL
    content: str

    @property
    def stream_id(self) -> str:
        return str(self.request_id)

    @property
    def body(self) -> str:
        return self.content


class ToolCallMessage(MessageEvent):
    """Завершённый tool call (id + имя + args)."""

    type: Literal["ToolCallMessage"] = "ToolCallMessage"
    stream_kind: Literal[StreamKind.TOOL_INVOCATION] = StreamKind.TOOL_INVOCATION
    call: ToolCall

    @property
    def stream_id(self) -> str:
        return self.call.id

    @property
    def headline(self) -> str | None:
        return self.call.name

    @property
    def body(self) -> str:
        return self.call.args_json()


class ToolResultReady(MessageEvent):
    """Результат выполнения tool — вызов и результат."""

    type: Literal["ToolResultReady"] = "ToolResultReady"
    stream_kind: Literal[StreamKind.TOOL_RESULT] = StreamKind.TOOL_RESULT
    call: ToolCall
    result: ToolCallResult

    @property
    def stream_id(self) -> str:
        return self.call.id

    @property
    def headline(self) -> str | None:
        return self.call.name

    @property
    def body(self) -> str:
        match self.result.result:
            case TextResult(text=t):
                return t
            case JsonResult(payload=p):
                return json.dumps(p, ensure_ascii=False)
            case TableResult(rows=r, note=n):
                body = json.dumps(r, ensure_ascii=False)
                return body if n is None else f"{body}\n\n{n}"
            case PgCopyTextResult(text=t):
                return t
            case ChartResult(title=title):
                # В историю/LLM уходит сводка, а не сырой Plotly-spec
                # (симметрично `tool_result_to_message`).
                return f"[chart rendered: {title}]" if title else "[chart rendered]"
            case ErrorResult(message=m):
                return m


class FeedbackToLLMAdded(MessageEvent):
    """Feedback от агента к LLM зарегистрирован в журнале HistoryService."""

    type: Literal["FeedbackToLLMAdded"] = "FeedbackToLLMAdded"
    stream_kind: Literal[StreamKind.FEEDBACK] = StreamKind.FEEDBACK
    content: str

    @property
    def stream_id(self) -> str:
        return str(self.request_id)

    @property
    def body(self) -> str:
        return self.content


class ToolCallDecodeFailedMessage(MessageEvent):
    """Tool-call, чьи args не декодировались — часть сообщения ассистента.

    Это контент (лежит в `message.tool_call_decode_failures`), а не advisory:
    «ошибочность» несёт сам payload (`failure.error`), а не категория.
    """

    type: Literal["ToolCallDecodeFailedMessage"] = "ToolCallDecodeFailedMessage"
    stream_kind: Literal[StreamKind.TOOL_INVOCATION] = StreamKind.TOOL_INVOCATION
    failure: ToolCallDecodeFailure

    @property
    def stream_id(self) -> str:
        return self.failure.id

    @property
    def headline(self) -> str | None:
        return self.failure.name

    @property
    def body(self) -> str:
        return f"raw: {self.failure.raw}\nerror: {self.failure.error}"


# --------------------------------------------------------------------- #
# AdvisoryEvent — конкретные события
# --------------------------------------------------------------------- #


class ToolExecutionFailed(AdvisoryEvent):
    """Tool упал — вызов и описание провала; цикл продолжается."""

    type: Literal["ToolExecutionFailed"] = "ToolExecutionFailed"
    call: ToolCall
    failure: ToolCallFailure

    @property
    def headline(self) -> str:
        return f"tool failed: {self.call.name}"

    @property
    def details(self) -> Mapping[str, str]:
        out: dict[str, str] = {
            "id": self.call.id,
            "name": self.call.name,
            "kind": self.failure.error_kind,
        }
        if self.status_code is not None:
            out["status_code"] = str(self.status_code)
        return out

    @property
    def body(self) -> str | None:
        primary = f"args: {self.call.args_json()}\nerror: {self.failure.message}"
        return compose_error_body(primary, self.status_code, self.cause_chain)


# --------------------------------------------------------------------- #
# TerminalEvent — конкретные события
# --------------------------------------------------------------------- #


class GenerationFailed(TerminalEvent):
    """LLM-слой бросил LLMError."""

    type: Literal["GenerationFailed"] = "GenerationFailed"
    message: str

    @property
    def headline(self) -> str:
        return f"generation failed: {self.error_kind}"

    @property
    def details(self) -> Mapping[str, str]:
        return _error_details(self.error_kind, self.status_code)

    @property
    def body(self) -> str | None:
        return compose_error_body(self.message, self.status_code, self.cause_chain)


class PromptFailed(TerminalEvent):
    """PromptFactory не смогла собрать system-prompt."""

    type: Literal["PromptFailed"] = "PromptFailed"
    message: str
    provider: str | None = None

    @property
    def headline(self) -> str:
        return f"prompt failed: {self.provider or 'unknown'}"

    @property
    def details(self) -> Mapping[str, str]:
        out = _error_details(self.error_kind, self.status_code)
        out["provider"] = self.provider or ""
        return out

    @property
    def body(self) -> str | None:
        return compose_error_body(self.message, self.status_code, self.cause_chain)


class MaxIterationsReached(TerminalEvent):
    """Цикл агента исчерпал лимит итераций без финального ответа."""

    type: Literal["MaxIterationsReached"] = "MaxIterationsReached"
    message: str
    limit: int
    iteration_count: int

    @property
    def headline(self) -> str:
        return f"max iterations: {self.iteration_count}/{self.limit}"

    @property
    def details(self) -> Mapping[str, str]:
        out = _error_details(self.error_kind, self.status_code)
        out["limit"] = str(self.limit)
        out["iteration"] = str(self.iteration_count)
        return out

    @property
    def body(self) -> str | None:
        return compose_error_body(self.message, self.status_code, self.cause_chain)


class PersistenceFailed(TerminalEvent):
    """Не удалось прочитать/записать journal/хранилище."""

    type: Literal["PersistenceFailed"] = "PersistenceFailed"
    message: str

    @property
    def headline(self) -> str:
        return f"persistence failed: {self.error_kind}"

    @property
    def details(self) -> Mapping[str, str]:
        return _error_details(self.error_kind, self.status_code)

    @property
    def body(self) -> str | None:
        return compose_error_body(self.message, self.status_code, self.cause_chain)


# --------------------------------------------------------------------- #
# Fallback для неизвестных type'ов (extension-модули)
# --------------------------------------------------------------------- #


class UnknownAgentEvent(AgentEventBase):
    """Fallback для события с неизвестным `type`.

    Decoder возвращает этот класс, когда `type` отсутствует в реестре.
    Sink, диспатчащий по `category`, продолжает работать; конкретный
    payload доступен через `payload`.
    """

    type: str
    category: EventCategory
    payload: Mapping[str, Any] = Field(default_factory=dict)


# --------------------------------------------------------------------- #
# Sealed union (для match-exhaustiveness внутри агента) + decoder
# --------------------------------------------------------------------- #


AgentEvent = (
    # PhaseEvent
    IterationStarted
    | ToolExecutionStarted
    | TotalMessage
    # DeltaEvent
    | ThinkingDelta
    | AnswerDelta
    | RefusalDelta
    | ToolCallDelta
    # MessageEvent
    | UserQueryReceived
    | ThinkingMessage
    | AnswerMessage
    | RefusalMessage
    | ToolCallMessage
    | ToolCallDecodeFailedMessage
    | ToolResultReady
    | FeedbackToLLMAdded
    # AdvisoryEvent
    | ToolExecutionFailed
    # TerminalEvent
    | GenerationFailed
    | PromptFailed
    | MaxIterationsReached
    | PersistenceFailed
)


AgentEventName: TypeAlias = Literal[
    "IterationStarted",
    "ToolExecutionStarted",
    "TotalMessage",
    "ThinkingDelta",
    "AnswerDelta",
    "RefusalDelta",
    "ToolCallDelta",
    "UserQueryReceived",
    "ThinkingMessage",
    "AnswerMessage",
    "RefusalMessage",
    "ToolCallMessage",
    "ToolResultReady",
    "FeedbackToLLMAdded",
    "ToolExecutionFailed",
    "ToolCallDecodeFailedMessage",
    "GenerationFailed",
    "PromptFailed",
    "MaxIterationsReached",
    "PersistenceFailed",
]


AgentEventAdapter: TypeAdapter[AgentEvent] = TypeAdapter(
    Annotated[AgentEvent, Field(discriminator="type")],
)
"""TypeAdapter для core-событий через discriminator='type'.

Использование:
    line: str = AgentEventAdapter.dump_json(event).decode("utf-8")
    event: AgentEvent = AgentEventAdapter.validate_json(line)

Для extension-friendly декодинга см. `AgentEventRegistry`.
"""


class AgentEventDecodeError(Exception):
    """Ошибка десериализации события из транспортного формата.

    Поднимается `AgentEventRegistry.decode` при невалидном payload
    (нет `type`, не проходит pydantic-валидация и т.п.). Не наследуется
    от `RoutableError`, т.к. decode выполняется транспортным слоем,
    а не агентским loop'ом — обработка остаётся за вызывающей стороной.
    """


class AgentEventRegistry:
    """Расширяемый реестр типов AgentEvent.

    Core-события зарегистрированы при импорте модуля. Extension-модуль
    может вызвать `register()` со своим классом события. `decode()` для
    неизвестного `type` возвращает `UnknownAgentEvent` с известной
    категорией, чтобы sink на категориях продолжал работать.
    """

    _by_type: ClassVar[dict[str, type[AgentEventBase]]] = {}

    @classmethod
    def register(cls, event_cls: type[AgentEventBase]) -> None:
        """Зарегистрировать класс события по его `type`-литералу."""
        type_field = event_cls.model_fields.get("type")
        if type_field is None or type_field.default is None:
            msg = (
                f"AgentEventRegistry: класс {event_cls.__name__} не имеет "
                f"`type`-литерала с дефолтом"
            )
            raise ValueError(msg)
        cls._by_type[str(type_field.default)] = event_cls

    @classmethod
    def get(cls, type_value: str) -> type[AgentEventBase] | None:
        return cls._by_type.get(type_value)

    @classmethod
    def types(cls) -> tuple[str, ...]:
        return tuple(cls._by_type.keys())

    @classmethod
    def decode(cls, data: Mapping[str, Any]) -> AgentEventBase:
        """Десериализовать событие по `type` из словаря.

        Неизвестный `type` → `UnknownAgentEvent` (с сохранённой category).
        """
        type_value = data.get("type")
        if not isinstance(type_value, str):
            raise AgentEventDecodeError(
                "AgentEventRegistry.decode: отсутствует поле 'type'"
            )
        event_cls = cls._by_type.get(type_value)
        if event_cls is None:
            try:
                return UnknownAgentEvent.model_validate(
                    {
                        "type": type_value,
                        "category": data.get("category"),
                        "request_id": data.get("request_id"),
                        "seq": data.get("seq", _UNSTAMPED_SEQ),
                        "emitted_at": data.get("emitted_at", _epoch_utc()),
                        "iteration": data.get("iteration", _UNSTAMPED_ITERATION),
                        "payload": {
                            k: v
                            for k, v in data.items()
                            if k
                            not in {
                                "type",
                                "category",
                                "request_id",
                                "seq",
                                "emitted_at",
                                "iteration",
                            }
                        },
                    }
                )
            except ValidationError as e:
                raise AgentEventDecodeError(
                    f"AgentEventRegistry.decode: невалидный UnknownAgentEvent "
                    f"для type={type_value!r}: {e}"
                ) from e
        try:
            return event_cls.model_validate(data)
        except ValidationError as e:
            raise AgentEventDecodeError(
                f"AgentEventRegistry.decode: невалидный payload для "
                f"type={type_value!r} ({event_cls.__name__}): {e}"
            ) from e


def _register_core_events() -> None:
    for cls in (
        IterationStarted,
        ToolExecutionStarted,
        TotalMessage,
        ThinkingDelta,
        AnswerDelta,
        RefusalDelta,
        ToolCallDelta,
        UserQueryReceived,
        ThinkingMessage,
        AnswerMessage,
        RefusalMessage,
        ToolCallMessage,
        ToolCallDecodeFailedMessage,
        ToolResultReady,
        FeedbackToLLMAdded,
        ToolExecutionFailed,
        GenerationFailed,
        PromptFailed,
        MaxIterationsReached,
        PersistenceFailed,
    ):
        AgentEventRegistry.register(cls)


_register_core_events()


def _verify_agent_event_names_exhaustive(e: AgentEvent) -> AgentEventName:
    """Compile-time гарантия синхронности union и Literal-имён."""
    return e.type  # type: ignore[return-value]
