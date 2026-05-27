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

   Пять «доменных» категорий (`PhaseEvent`, `ContentDeltaEvent`,
   `ContentSnapshotEvent`, `AdvisoryEvent`, `TerminalEvent`) несут информацию,
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

    `ContentDeltaEvent`
        инкрементальный кусок контента, который стримится в открытый «слот» UI.
        Соединяется с `ContentSnapshotEvent` через общий `stream_id`:
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
            `part`          - для TOOL_INVOCATION — args | result

    `ContentSnapshotEvent`
        завершённое сообщение в диалоге.
        Финальная форма того, что собиралось из delta:
        - `AnswerComplete` после серии `AnswerDelta`

        либо самостоятельное снапшот-событие, у
        которого стриминга не было
            `ToolCallComplete`      - завершение вызова tool
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
                TOOL_INVOCATION
                ...
            `body`          - агрегированный контент
            `headline`      - опциональный заголовок — для tool это имя инструмента
            `part`          - для TOOL_INVOCATION — args | result

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
from boba.llm.models import AssistantMessage, InvalidToolCall, RequestId, ToolCall
from boba.tools.domain import ErrorResult, JsonResult, TextResult

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
    CONTENT_DELTA = "content_delta"
    CONTENT_SNAPSHOT = "content_snapshot"
    ADVISORY = "advisory"
    TERMINAL = "terminal"
    DIAGNOSTIC = "diagnostic"


class StreamKind(StrEnum):
    """Тип потока контента — куда стримятся delta и snapshot"""

    USER_QUERY = "user_query"
    THINKING = "thinking"
    ANSWER = "answer"
    REFUSAL = "refusal"
    FEEDBACK = "feedback"
    TOOL_INVOCATION = "tool_invocation"


class ToolPart(StrEnum):
    """Часть жизненного цикла tool-вызова в рамках одной сущности."""

    ARGS = "args"
    RESULT = "result"


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

    type: str
    category: EventCategory
    request_id: RequestId
    seq: int = _UNSTAMPED_SEQ
    emitted_at: datetime = Field(default_factory=_epoch_utc)
    iteration: int = _UNSTAMPED_ITERATION


class PhaseEvent(AgentEventBase):
    """
    Граница фазы в round-trip (туда-обратно)
    """

    category: Literal[EventCategory.PHASE] = EventCategory.PHASE

    label: str = ""
    severity: Severity = Severity.INFO
    details: Mapping[str, str] = Field(default_factory=dict)
    body: str | None = None
    # Наносекунды с момента предыдущего PhaseEvent того же request_id.
    # Стампится `EventStamperMiddleware` через `time.monotonic_ns()`.
    # Для первого PhaseEvent в request — 0.
    duration_ns: int = 0


class ContentDeltaEvent(AgentEventBase):
    """
    Инкрементальный кусок в поток — `stream_id` + `stream_kind` + `chunk`
    """

    category: Literal[EventCategory.CONTENT_DELTA] = EventCategory.CONTENT_DELTA

    stream_id: str = ""
    stream_kind: StreamKind = StreamKind.ANSWER
    chunk: str = ""
    part: ToolPart | None = None


class ContentSnapshotEvent(AgentEventBase):
    """
    Завершённое сообщение — `stream_id` + `stream_kind` + `body`
    """

    category: Literal[EventCategory.CONTENT_SNAPSHOT] = EventCategory.CONTENT_SNAPSHOT

    stream_id: str = ""
    stream_kind: StreamKind = StreamKind.ANSWER
    headline: str | None = None
    body: str = ""
    part: ToolPart | None = None


class AdvisoryEvent(AgentEventBase):
    """
    Нефатальное уведомление (но некая ошибка)
    агентский цикл бдет продолжен

    `status_code` и `cause_chain` авто-проставляются `AgentErrorRouter`'ом
    из исключения; концы цепочки видны в `body` через `compose_error_body`.
    """

    category: Literal[EventCategory.ADVISORY] = EventCategory.ADVISORY

    headline: str = ""
    severity: Severity = Severity.WARN
    details: Mapping[str, str] = Field(default_factory=dict)
    body: str | None = None
    status_code: int | None = None
    cause_chain: tuple[str, ...] = ()


class TerminalEvent(AgentEventBase):
    """
    Фатальное уведомление
    агентский цикл будет остановлен

    `status_code` и `cause_chain` авто-проставляются `AgentErrorRouter`'ом
    из исключения; концы цепочки видны в `body` через `compose_error_body`.
    """

    category: Literal[EventCategory.TERMINAL] = EventCategory.TERMINAL

    headline: str = ""
    severity: Severity = Severity.ERROR
    details: Mapping[str, str] = Field(default_factory=dict)
    body: str | None = None
    error_kind: str = ""
    status_code: int | None = None
    cause_chain: tuple[str, ...] = ()


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
    args+error, raw_args+error), а добавление `status` и `Caused by:` —
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

    @model_validator(mode="after")
    def _derive(self) -> Self:
        self.label = f"iteration {self.iteration_count}/{self.max_iterations}"
        self.details = {
            "iteration": str(self.iteration_count),
            "max": str(self.max_iterations),
        }

        return self


class ToolExecutionStarted(PhaseEvent):
    """
    Tool готов к исполнению - PhaseEvent несёт только `tool_call_id` и
    `tool_name`. Полный `ToolCall` (с args) уносится в `ToolArgsResolved`
    (DiagnosticEvent) - продьюсер эмитит его рядом.
    """

    type: Literal["ToolExecutionStarted"] = "ToolExecutionStarted"
    tool_call_id: str
    tool_name: str

    @model_validator(mode="after")
    def _derive(self) -> Self:
        self.label = f"tool exec: {self.tool_name}"
        self.details = {"id": self.tool_call_id, "name": self.tool_name}
        return self


class GenerationDone(PhaseEvent):
    """
    Прогон завершён — пришёл finish_reason

    Поле `finish_reason` — то, что реально прислал провайдер на wire,
    без подмен. Решения о терминальности агентского цикла принимаются
    выше — на отдельных `StopIf*` спецификациях, читающих агрегированный
    `GenerationResult`.
    """

    type: Literal["GenerationDone"] = "GenerationDone"
    finish_reason: FinishReason = FinishReason.STOP

    @model_validator(mode="after")
    def _derive(self) -> Self:
        self.label = f"generation done ({self.finish_reason.value})"
        self.details = {"finish_reason": self.finish_reason.value}
        return self


class GenerationResult(PhaseEvent):
    """
    Итог одной генерации LLM — собранный AssistantMessage и raw finish_reason.

    Эмитится строго после `GenerationDone` и после всех per-field
    `*Complete`/`InvalidToolCallReceived` событий. Несёт всё, что пришло
    от провайдера в режиме streaming, в форме обычного `stream=False` ответа:

    - `message` — собранный AssistantMessage со всеми блоками
      (thinking / text / refusal / tool_calls / invalid_tool_calls).
      Пустой message (без блоков) — валидное состояние, когда модель
      завершилась без контента.
    - `finish_reason` — то, что реально прислал провайдер; без подмен.

    Единственное событие, на котором принимается решение об остановке
    агентского цикла — см. `StopIfNotToolCall`, `StopIfLengthReached`,
    `StopIfContentFilter`.
    """

    type: Literal["GenerationResult"] = "GenerationResult"
    message: AssistantMessage
    finish_reason: FinishReason

    @model_validator(mode="after")
    def _derive(self) -> Self:
        n_blocks = len(self.message.blocks)
        n_tools = len(self.message.tool_calls)
        self.label = (
            f"generation result ({self.finish_reason.value}, "
            f"blocks={n_blocks}, tool_calls={n_tools})"
        )
        self.details = {
            "finish_reason": self.finish_reason.value,
            "blocks": str(n_blocks),
            "tool_calls": str(n_tools),
            "has_answer": str(bool(self.message.content)),
            "has_thinking": str(bool(self.message.thinking)),
            "has_refusal": str(bool(self.message.refusal)),
        }
        # Severity подсказывает sink'ам визуальный приоритет:
        # LENGTH — ответ обрезан, есть смысл показать предупреждение;
        # CONTENT_FILTER — провайдер заблокировал, ошибка для пользователя;
        # STOP / TOOL_CALLS — обычный финал, INFO.
        self.severity = _severity_for_finish_reason(self.finish_reason)
        return self


# --------------------------------------------------------------------- #
# ContentDeltaEvent — конкретные события
# --------------------------------------------------------------------- #


class ThinkingDelta(ContentDeltaEvent):
    """
    Chunk reasoning-токена
    """

    type: Literal["ThinkingDelta"] = "ThinkingDelta"
    stream_kind: Literal[StreamKind.THINKING] = StreamKind.THINKING
    token: str

    @model_validator(mode="after")
    def _derive(self) -> Self:
        self.stream_id = str(self.request_id)
        self.chunk = self.token
        return self


class AnswerDelta(ContentDeltaEvent):
    """
    Chunk текстового ответа для отображения пользователю
    """

    type: Literal["AnswerDelta"] = "AnswerDelta"
    stream_kind: Literal[StreamKind.ANSWER] = StreamKind.ANSWER
    token: str

    @model_validator(mode="after")
    def _derive(self) -> Self:
        self.stream_id = str(self.request_id)
        self.chunk = self.token
        return self


class RefusalDelta(ContentDeltaEvent):
    """
    Chunk отказа модели отвечать
    """

    type: Literal["RefusalDelta"] = "RefusalDelta"
    stream_kind: Literal[StreamKind.REFUSAL] = StreamKind.REFUSAL
    token: str

    @model_validator(mode="after")
    def _derive(self) -> Self:
        self.stream_id = str(self.request_id)
        self.chunk = self.token
        return self


class ToolCallDelta(ContentDeltaEvent):
    """
    Chunk аргументов tool call
    tool call тоже приходит частами
    """

    type: Literal["ToolCallDelta"] = "ToolCallDelta"
    stream_kind: Literal[StreamKind.TOOL_INVOCATION] = StreamKind.TOOL_INVOCATION
    part: Literal[ToolPart.ARGS] = ToolPart.ARGS
    index: int
    tool_call_id: str
    tool_name: str
    arguments_chunk: str

    @model_validator(mode="after")
    def _derive(self) -> Self:
        self.stream_id = self.tool_call_id
        self.chunk = self.arguments_chunk
        return self


# --------------------------------------------------------------------- #
# ContentSnapshotEvent — конкретные события
# --------------------------------------------------------------------- #


class UserQueryReceived(ContentSnapshotEvent):
    """Запрос пользователя принят агентом и записан в историю."""

    type: Literal["UserQueryReceived"] = "UserQueryReceived"
    stream_kind: Literal[StreamKind.USER_QUERY] = StreamKind.USER_QUERY
    query: str

    @model_validator(mode="after")
    def _derive(self) -> Self:
        self.stream_id = str(self.request_id)
        self.body = self.query
        return self


class ThinkingComplete(ContentSnapshotEvent):
    """Агрегированный reasoning итерации."""

    type: Literal["ThinkingComplete"] = "ThinkingComplete"
    stream_kind: Literal[StreamKind.THINKING] = StreamKind.THINKING
    content: str

    @model_validator(mode="after")
    def _derive(self) -> Self:
        self.stream_id = str(self.request_id)
        self.body = self.content
        return self


class AnswerComplete(ContentSnapshotEvent):
    """Агрегированный текстовый ответ итерации"""

    type: Literal["AnswerComplete"] = "AnswerComplete"
    stream_kind: Literal[StreamKind.ANSWER] = StreamKind.ANSWER
    content: str

    @model_validator(mode="after")
    def _derive(self) -> Self:
        self.stream_id = str(self.request_id)
        self.body = self.content
        return self


class RefusalComplete(ContentSnapshotEvent):
    """Агрегированный отказ модели."""

    type: Literal["RefusalComplete"] = "RefusalComplete"
    stream_kind: Literal[StreamKind.REFUSAL] = StreamKind.REFUSAL
    content: str

    @model_validator(mode="after")
    def _derive(self) -> Self:
        self.stream_id = str(self.request_id)
        self.body = self.content
        return self


class ToolCallComplete(ContentSnapshotEvent):
    """Завершённый tool call (id + имя + args)."""

    type: Literal["ToolCallComplete"] = "ToolCallComplete"
    stream_kind: Literal[StreamKind.TOOL_INVOCATION] = StreamKind.TOOL_INVOCATION
    part: Literal[ToolPart.ARGS] = ToolPart.ARGS
    call: ToolCall

    @model_validator(mode="after")
    def _derive(self) -> Self:
        self.stream_id = self.call.id
        self.headline = self.call.name
        self.body = self.call.args_json()
        return self


class ToolResultReady(ContentSnapshotEvent):
    """Результат выполнения tool — вызов и результат."""

    type: Literal["ToolResultReady"] = "ToolResultReady"
    stream_kind: Literal[StreamKind.TOOL_INVOCATION] = StreamKind.TOOL_INVOCATION
    part: Literal[ToolPart.RESULT] = ToolPart.RESULT
    call: ToolCall
    result: ToolCallResult

    @model_validator(mode="after")
    def _derive(self) -> Self:
        self.stream_id = self.call.id
        self.headline = self.call.name
        match self.result.result:
            case TextResult(text=t):
                self.body = t
            case JsonResult(payload=p):
                self.body = json.dumps(p, ensure_ascii=False)
            case ErrorResult(message=m):
                self.body = m
        return self


class FeedbackToLLMAdded(ContentSnapshotEvent):
    """Feedback от агента к LLM зарегистрирован в журнале HistoryService."""

    type: Literal["FeedbackToLLMAdded"] = "FeedbackToLLMAdded"
    stream_kind: Literal[StreamKind.FEEDBACK] = StreamKind.FEEDBACK
    content: str

    @model_validator(mode="after")
    def _derive(self) -> Self:
        self.stream_id = str(self.request_id)
        self.body = self.content
        return self


# --------------------------------------------------------------------- #
# AdvisoryEvent — конкретные события
# --------------------------------------------------------------------- #


class InvalidToolCallReceived(AdvisoryEvent):
    """LLM выдала tool-call с невалидным JSON в args; цикл продолжается."""

    type: Literal["InvalidToolCallReceived"] = "InvalidToolCallReceived"
    invalid: InvalidToolCall

    @model_validator(mode="after")
    def _derive(self) -> Self:
        self.headline = f"invalid tool call: {self.invalid.name}"
        details: dict[str, str] = {
            "id": self.invalid.id,
            "name": self.invalid.name,
        }
        if self.status_code is not None:
            details["status_code"] = str(self.status_code)
        self.details = details
        primary = f"raw_args: {self.invalid.raw_args}\nerror: {self.invalid.error}"
        self.body = compose_error_body(primary, self.status_code, self.cause_chain)
        return self


class ToolExecutionFailed(AdvisoryEvent):
    """Tool упал — вызов и описание провала; цикл продолжается."""

    type: Literal["ToolExecutionFailed"] = "ToolExecutionFailed"
    call: ToolCall
    failure: ToolCallFailure

    @model_validator(mode="after")
    def _derive(self) -> Self:
        self.headline = f"tool failed: {self.call.name}"
        details: dict[str, str] = {
            "id": self.call.id,
            "name": self.call.name,
            "kind": self.failure.error_kind,
        }
        if self.status_code is not None:
            details["status_code"] = str(self.status_code)
        self.details = details
        primary = f"args: {self.call.args_json()}\nerror: {self.failure.message}"
        self.body = compose_error_body(primary, self.status_code, self.cause_chain)
        return self


# --------------------------------------------------------------------- #
# TerminalEvent — конкретные события
# --------------------------------------------------------------------- #


class GenerationFailed(TerminalEvent):
    """LLM-слой бросил LLMError."""

    type: Literal["GenerationFailed"] = "GenerationFailed"
    message: str

    @model_validator(mode="after")
    def _derive(self) -> Self:
        self.headline = f"generation failed: {self.error_kind}"
        self.details = _error_details(self.error_kind, self.status_code)
        self.body = compose_error_body(
            self.message, self.status_code, self.cause_chain,
        )
        return self


class PromptFailed(TerminalEvent):
    """PromptFactory не смогла собрать system-prompt."""

    type: Literal["PromptFailed"] = "PromptFailed"
    message: str
    provider: str | None = None

    @model_validator(mode="after")
    def _derive(self) -> Self:
        self.headline = f"prompt failed: {self.provider or 'unknown'}"
        details = _error_details(self.error_kind, self.status_code)
        details["provider"] = self.provider or ""
        self.details = details
        self.body = compose_error_body(
            self.message, self.status_code, self.cause_chain,
        )
        return self


class MaxIterationsReached(TerminalEvent):
    """Цикл агента исчерпал лимит итераций без финального ответа."""

    type: Literal["MaxIterationsReached"] = "MaxIterationsReached"
    message: str
    limit: int
    iteration_count: int

    @model_validator(mode="after")
    def _derive(self) -> Self:
        self.headline = f"max iterations: {self.iteration_count}/{self.limit}"
        details = _error_details(self.error_kind, self.status_code)
        details["limit"] = str(self.limit)
        details["iteration"] = str(self.iteration_count)
        self.details = details
        self.body = compose_error_body(
            self.message, self.status_code, self.cause_chain,
        )
        return self


class PersistenceFailed(TerminalEvent):
    """Не удалось прочитать/записать journal/хранилище."""

    type: Literal["PersistenceFailed"] = "PersistenceFailed"
    message: str

    @model_validator(mode="after")
    def _derive(self) -> Self:
        self.headline = f"persistence failed: {self.error_kind}"
        self.details = _error_details(self.error_kind, self.status_code)
        self.body = compose_error_body(
            self.message, self.status_code, self.cause_chain,
        )
        return self


# --------------------------------------------------------------------- #
# DiagnosticEvent — конкретные события
# --------------------------------------------------------------------- #


class ToolArgsResolved(DiagnosticEvent):
    """Полный `ToolCall` с args - готов к исполнению.

    Бремя args вынесено из `ToolExecutionStarted` (PhaseEvent): args
    уже есть в `ToolCallComplete` (ContentSnapshotEvent), и PhaseEvent
    их дублировал в `body`. Здесь они доступны для diagnostic-режима
    - привязка к tool-step'у через `related["tool_call_id"]`.
    """

    type: Literal["ToolArgsResolved"] = "ToolArgsResolved"
    call: ToolCall

    @model_validator(mode="after")
    def _derive(self) -> Self:
        self.topic = "tool.args_resolved"
        self.headline = f"tool args: {self.call.name}"
        self.details = {"id": self.call.id, "name": self.call.name}
        self.body = self.call.args_json()
        self.related = {"tool_call_id": self.call.id}
        return self


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
    | GenerationDone
    | GenerationResult
    # ContentDeltaEvent
    | ThinkingDelta
    | AnswerDelta
    | RefusalDelta
    | ToolCallDelta
    # ContentSnapshotEvent
    | UserQueryReceived
    | ThinkingComplete
    | AnswerComplete
    | RefusalComplete
    | ToolCallComplete
    | ToolResultReady
    | FeedbackToLLMAdded
    # AdvisoryEvent
    | ToolExecutionFailed
    | InvalidToolCallReceived
    # TerminalEvent
    | GenerationFailed
    | PromptFailed
    | MaxIterationsReached
    | PersistenceFailed
    # DiagnosticEvent
    | ToolArgsResolved
)


AgentEventName: TypeAlias = Literal[
    "IterationStarted",
    "ToolExecutionStarted",
    "GenerationDone",
    "GenerationResult",
    "ThinkingDelta",
    "AnswerDelta",
    "RefusalDelta",
    "ToolCallDelta",
    "UserQueryReceived",
    "ThinkingComplete",
    "AnswerComplete",
    "RefusalComplete",
    "ToolCallComplete",
    "ToolResultReady",
    "FeedbackToLLMAdded",
    "ToolExecutionFailed",
    "InvalidToolCallReceived",
    "GenerationFailed",
    "PromptFailed",
    "MaxIterationsReached",
    "PersistenceFailed",
    "ToolArgsResolved",
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
        GenerationDone,
        GenerationResult,
        ThinkingDelta,
        AnswerDelta,
        RefusalDelta,
        ToolCallDelta,
        UserQueryReceived,
        ThinkingComplete,
        AnswerComplete,
        RefusalComplete,
        ToolCallComplete,
        ToolResultReady,
        FeedbackToLLMAdded,
        ToolExecutionFailed,
        InvalidToolCallReceived,
        GenerationFailed,
        PromptFailed,
        MaxIterationsReached,
        PersistenceFailed,
        ToolArgsResolved,
    ):
        AgentEventRegistry.register(cls)


_register_core_events()


def _verify_agent_event_names_exhaustive(e: AgentEvent) -> AgentEventName:
    """Compile-time гарантия синхронности union и Literal-имён."""
    return e.type  # type: ignore[return-value]
