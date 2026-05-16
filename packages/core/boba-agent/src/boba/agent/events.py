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
        - `AnswerComplete` после серии `AnswerToken`

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

from collections.abc import Mapping
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Any, ClassVar, Final, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator

from boba.agent.models import (
    ToolCallFailure,
    ToolCallResult,
)
from boba.llm.events import FinishReason
from boba.llm.models import InvalidToolCall, RequestId, ToolCall
from boba.tools.domain import DefaultTextVisitor

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

    model_config = ConfigDict(frozen=True, extra="forbid")

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
    """

    category: Literal[EventCategory.ADVISORY] = EventCategory.ADVISORY

    headline: str = ""
    severity: Severity = Severity.WARN
    details: Mapping[str, str] = Field(default_factory=dict)
    body: str | None = None


class TerminalEvent(AgentEventBase):
    """
    Фатальное уведомление
    агентский цикл будет остановлен
    """

    category: Literal[EventCategory.TERMINAL] = EventCategory.TERMINAL

    headline: str = ""
    severity: Severity = Severity.ERROR
    details: Mapping[str, str] = Field(default_factory=dict)
    body: str | None = None
    error_kind: str = ""


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

    @model_validator(mode="before")
    @classmethod
    def _derive(cls, data: Any) -> Any:
        if isinstance(data, dict):
            it = data.get("iteration_count")
            mx = data.get("max_iterations")
            data.setdefault("label", f"iteration {it}/{mx}")
            data.setdefault("details", {"iteration": str(it), "max": str(mx)})
        return data


class LLMRequestSent(PhaseEvent):
    """Round-trip к LLM начат."""

    type: Literal["LLMRequestSent"] = "LLMRequestSent"
    model: str
    messages_count: int
    has_tools: bool
    monotonic_ns: int

    @model_validator(mode="before")
    @classmethod
    def _derive(cls, data: Any) -> Any:
        if isinstance(data, dict):
            model = data.get("model")
            cnt = data.get("messages_count")
            tools = " +tools" if data.get("has_tools") else ""
            data.setdefault("label", f"→ llm: {model}, {cnt} msgs{tools}")
            data.setdefault(
                "details",
                {
                    "model": str(model),
                    "messages_count": str(cnt),
                    "has_tools": str(data.get("has_tools")),
                },
            )
        return data


class LLMResponseStreamOpened(PhaseEvent):
    """Stream-handle от провайдера получен — парный замер к LLMRequestSent."""

    type: Literal["LLMResponseStreamOpened"] = "LLMResponseStreamOpened"
    monotonic_ns: int

    @model_validator(mode="before")
    @classmethod
    def _derive(cls, data: Any) -> Any:
        if isinstance(data, dict):
            data.setdefault("label", "← stream open")
        return data


class GenerationStarted(PhaseEvent):
    """Первый chunk от LLM — генерация началась."""

    type: Literal["GenerationStarted"] = "GenerationStarted"

    @model_validator(mode="before")
    @classmethod
    def _derive(cls, data: Any) -> Any:
        if isinstance(data, dict):
            data.setdefault("label", "generation")
        return data


class ThinkingStarted(PhaseEvent):
    """Модель начала reasoning."""

    type: Literal["ThinkingStarted"] = "ThinkingStarted"

    @model_validator(mode="before")
    @classmethod
    def _derive(cls, data: Any) -> Any:
        if isinstance(data, dict):
            data.setdefault("label", "thinking")
        return data


class AnswerStarted(PhaseEvent):
    """Модель начала отдавать ответ."""

    type: Literal["AnswerStarted"] = "AnswerStarted"

    @model_validator(mode="before")
    @classmethod
    def _derive(cls, data: Any) -> Any:
        if isinstance(data, dict):
            data.setdefault("label", "answer")
        return data


class ToolCallStreamStarted(PhaseEvent):
    """Tool call объявлен — id и имя пришли, args ещё стримятся."""

    type: Literal["ToolCallStreamStarted"] = "ToolCallStreamStarted"
    index: int
    tool_call_id: str
    tool_name: str

    @model_validator(mode="before")
    @classmethod
    def _derive(cls, data: Any) -> Any:
        if isinstance(data, dict):
            idx = data.get("index")
            name = data.get("tool_name")
            tid = data.get("tool_call_id")
            data.setdefault("label", f"tool#{idx} stream: {name}")
            data.setdefault(
                "details",
                {
                    "id": str(tid),
                    "name": str(name),
                    "index": str(idx),
                },
            )
        return data


class ToolExecutionStarted(PhaseEvent):
    """Tool готов к исполнению — args разобраны."""

    type: Literal["ToolExecutionStarted"] = "ToolExecutionStarted"
    call: ToolCall

    @model_validator(mode="before")
    @classmethod
    def _derive(cls, data: Any) -> Any:
        if isinstance(data, dict):
            call = data.get("call")
            if isinstance(call, ToolCall):
                data.setdefault("label", f"tool exec: {call.name}")
                data.setdefault("details", {"id": call.id, "name": call.name})
                data.setdefault("body", call.args_json())
        return data


class GenerationRetried(PhaseEvent):
    """LLM-слой решил повторить запрос."""

    type: Literal["GenerationRetried"] = "GenerationRetried"
    attempt: int
    reason: str
    status_code: int | None = None
    severity: Severity = Severity.WARN

    @model_validator(mode="before")
    @classmethod
    def _derive(cls, data: Any) -> Any:
        if isinstance(data, dict):
            att = data.get("attempt")
            rsn = data.get("reason")
            sc = data.get("status_code")
            data.setdefault("label", f"retry #{att}: {rsn}")
            data.setdefault(
                "details",
                {
                    "attempt": str(att),
                    "reason": str(rsn),
                    "status_code": str(sc) if sc is not None else "",
                },
            )
        return data


class GenerationDone(PhaseEvent):
    """Прогон завершён — пришёл finish_reason."""

    type: Literal["GenerationDone"] = "GenerationDone"
    finish_reason: FinishReason = FinishReason.STOP

    @model_validator(mode="before")
    @classmethod
    def _derive(cls, data: Any) -> Any:
        if isinstance(data, dict):
            fr = data.get("finish_reason", FinishReason.STOP)
            fr_value = fr.value if isinstance(fr, FinishReason) else str(fr)
            data.setdefault("label", f"generation done ({fr_value})")
            data.setdefault("details", {"finish_reason": fr_value})
        return data


# --------------------------------------------------------------------- #
# ContentDeltaEvent — конкретные события
# --------------------------------------------------------------------- #


class ThinkingToken(ContentDeltaEvent):
    """Chunk reasoning-токена."""

    type: Literal["ThinkingToken"] = "ThinkingToken"
    token: str

    @model_validator(mode="before")
    @classmethod
    def _derive(cls, data: Any) -> Any:
        if isinstance(data, dict):
            data.setdefault("stream_kind", StreamKind.THINKING)
            data.setdefault("stream_id", str(data.get("request_id", "")))
            data.setdefault("chunk", str(data.get("token", "")))
        return data


class AnswerToken(ContentDeltaEvent):
    """Chunk текстового ответа для отображения пользователю."""

    type: Literal["AnswerToken"] = "AnswerToken"
    token: str

    @model_validator(mode="before")
    @classmethod
    def _derive(cls, data: Any) -> Any:
        if isinstance(data, dict):
            data.setdefault("stream_kind", StreamKind.ANSWER)
            data.setdefault("stream_id", str(data.get("request_id", "")))
            data.setdefault("chunk", str(data.get("token", "")))
        return data


class RefusalToken(ContentDeltaEvent):
    """Chunk отказа модели отвечать."""

    type: Literal["RefusalToken"] = "RefusalToken"
    token: str

    @model_validator(mode="before")
    @classmethod
    def _derive(cls, data: Any) -> Any:
        if isinstance(data, dict):
            data.setdefault("stream_kind", StreamKind.REFUSAL)
            data.setdefault("stream_id", str(data.get("request_id", "")))
            data.setdefault("chunk", str(data.get("token", "")))
        return data


class ToolCallArgumentDelta(ContentDeltaEvent):
    """Chunk аргументов tool call (JSON-строка, может прийти частями)."""

    type: Literal["ToolCallArgumentDelta"] = "ToolCallArgumentDelta"
    index: int
    tool_call_id: str
    tool_name: str
    arguments_chunk: str

    @model_validator(mode="before")
    @classmethod
    def _derive(cls, data: Any) -> Any:
        if isinstance(data, dict):
            data.setdefault("stream_kind", StreamKind.TOOL_INVOCATION)
            data.setdefault("stream_id", str(data.get("tool_call_id", "")))
            data.setdefault("chunk", str(data.get("arguments_chunk", "")))
            data.setdefault("part", ToolPart.ARGS)
        return data


# --------------------------------------------------------------------- #
# ContentSnapshotEvent — конкретные события
# --------------------------------------------------------------------- #


class UserQueryReceived(ContentSnapshotEvent):
    """Запрос пользователя принят агентом и записан в историю."""

    type: Literal["UserQueryReceived"] = "UserQueryReceived"
    query: str

    @model_validator(mode="before")
    @classmethod
    def _derive(cls, data: Any) -> Any:
        if isinstance(data, dict):
            data.setdefault("stream_kind", StreamKind.USER_QUERY)
            data.setdefault("stream_id", str(data.get("request_id", "")))
            data.setdefault("body", str(data.get("query", "")))
        return data


class ThinkingComplete(ContentSnapshotEvent):
    """Агрегированный reasoning итерации."""

    type: Literal["ThinkingComplete"] = "ThinkingComplete"
    content: str

    @model_validator(mode="before")
    @classmethod
    def _derive(cls, data: Any) -> Any:
        if isinstance(data, dict):
            data.setdefault("stream_kind", StreamKind.THINKING)
            data.setdefault("stream_id", str(data.get("request_id", "")))
            data.setdefault("body", str(data.get("content", "")))
        return data


class AnswerComplete(ContentSnapshotEvent):
    """Агрегированный текстовый ответ итерации (пишется в историю)."""

    type: Literal["AnswerComplete"] = "AnswerComplete"
    content: str

    @model_validator(mode="before")
    @classmethod
    def _derive(cls, data: Any) -> Any:
        if isinstance(data, dict):
            data.setdefault("stream_kind", StreamKind.ANSWER)
            data.setdefault("stream_id", str(data.get("request_id", "")))
            data.setdefault("body", str(data.get("content", "")))
        return data


class RefusalComplete(ContentSnapshotEvent):
    """Агрегированный отказ модели."""

    type: Literal["RefusalComplete"] = "RefusalComplete"
    content: str

    @model_validator(mode="before")
    @classmethod
    def _derive(cls, data: Any) -> Any:
        if isinstance(data, dict):
            data.setdefault("stream_kind", StreamKind.REFUSAL)
            data.setdefault("stream_id", str(data.get("request_id", "")))
            data.setdefault("body", str(data.get("content", "")))
        return data


class ToolCallComplete(ContentSnapshotEvent):
    """Завершённый tool call (id + имя + args)."""

    type: Literal["ToolCallComplete"] = "ToolCallComplete"
    call: ToolCall

    @model_validator(mode="before")
    @classmethod
    def _derive(cls, data: Any) -> Any:
        if isinstance(data, dict):
            call = data.get("call")
            if isinstance(call, ToolCall):
                data.setdefault("stream_kind", StreamKind.TOOL_INVOCATION)
                data.setdefault("stream_id", call.id)
                data.setdefault("headline", call.name)
                data.setdefault("body", call.args_json())
                data.setdefault("part", ToolPart.ARGS)
        return data


class ToolResultReady(ContentSnapshotEvent):
    """Результат выполнения tool — вызов и результат."""

    _TEXT_VISITOR: ClassVar[DefaultTextVisitor] = DefaultTextVisitor()

    type: Literal["ToolResultReady"] = "ToolResultReady"
    call: ToolCall
    result: ToolCallResult

    @model_validator(mode="before")
    @classmethod
    def _derive(cls, data: Any) -> Any:
        if isinstance(data, dict):
            call = data.get("call")
            result = data.get("result")
            if isinstance(call, ToolCall):
                data.setdefault("stream_kind", StreamKind.TOOL_INVOCATION)
                data.setdefault("stream_id", call.id)
                data.setdefault("headline", call.name)
                data.setdefault("part", ToolPart.RESULT)
            if isinstance(result, ToolCallResult) and "body" not in data:
                data["body"] = result.result.accept(cls._TEXT_VISITOR)
        return data


class FeedbackToLLMAdded(ContentSnapshotEvent):
    """Feedback от агента к LLM записан в MessageService."""

    type: Literal["FeedbackToLLMAdded"] = "FeedbackToLLMAdded"
    content: str

    @model_validator(mode="before")
    @classmethod
    def _derive(cls, data: Any) -> Any:
        if isinstance(data, dict):
            data.setdefault("stream_kind", StreamKind.FEEDBACK)
            data.setdefault("stream_id", str(data.get("request_id", "")))
            data.setdefault("body", str(data.get("content", "")))
        return data


# --------------------------------------------------------------------- #
# AdvisoryEvent — конкретные события
# --------------------------------------------------------------------- #


class InvalidToolCallReceived(AdvisoryEvent):
    """LLM выдала tool-call с невалидным JSON в args; цикл продолжается."""

    type: Literal["InvalidToolCallReceived"] = "InvalidToolCallReceived"
    invalid: InvalidToolCall

    @model_validator(mode="before")
    @classmethod
    def _derive(cls, data: Any) -> Any:
        if isinstance(data, dict):
            inv = data.get("invalid")
            if isinstance(inv, InvalidToolCall):
                data.setdefault("headline", f"invalid tool call: {inv.name}")
                data.setdefault("details", {"id": inv.id, "name": inv.name})
                data.setdefault(
                    "body",
                    f"raw_args: {inv.raw_args}\nerror: {inv.error}",
                )
        return data


class ToolExecutionFailed(AdvisoryEvent):
    """Tool упал — вызов и описание провала; цикл продолжается."""

    type: Literal["ToolExecutionFailed"] = "ToolExecutionFailed"
    call: ToolCall
    failure: ToolCallFailure

    @model_validator(mode="before")
    @classmethod
    def _derive(cls, data: Any) -> Any:
        if isinstance(data, dict):
            call = data.get("call")
            failure = data.get("failure")
            if isinstance(call, ToolCall) and isinstance(failure, ToolCallFailure):
                data.setdefault("headline", f"tool failed: {call.name}")
                data.setdefault(
                    "details",
                    {
                        "id": call.id,
                        "name": call.name,
                        "kind": failure.error_kind,
                    },
                )
                data.setdefault(
                    "body",
                    f"args: {call.args_json()}\nerror: {failure.message}",
                )
        return data


# --------------------------------------------------------------------- #
# TerminalEvent — конкретные события
# --------------------------------------------------------------------- #


class GenerationFailed(TerminalEvent):
    """LLM-слой бросил LLMError."""

    type: Literal["GenerationFailed"] = "GenerationFailed"
    message: str

    @model_validator(mode="before")
    @classmethod
    def _derive(cls, data: Any) -> Any:
        if isinstance(data, dict):
            kind = data.get("error_kind", "")
            data.setdefault("headline", f"generation failed: {kind}")
            data.setdefault("details", {"kind": str(kind)})
            data.setdefault("body", str(data.get("message", "")))
        return data


class PromptFailed(TerminalEvent):
    """PromptFactory не смогла собрать system-prompt."""

    type: Literal["PromptFailed"] = "PromptFailed"
    message: str
    provider: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _derive(cls, data: Any) -> Any:
        if isinstance(data, dict):
            kind = data.get("error_kind", "")
            provider = data.get("provider")
            data.setdefault("headline", f"prompt failed: {provider or 'unknown'}")
            data.setdefault(
                "details",
                {
                    "kind": str(kind),
                    "provider": str(provider) if provider else "",
                },
            )
            data.setdefault("body", str(data.get("message", "")))
        return data


class MaxIterationsReached(TerminalEvent):
    """Цикл агента исчерпал лимит итераций без финального ответа."""

    type: Literal["MaxIterationsReached"] = "MaxIterationsReached"
    message: str
    limit: int
    iteration_count: int

    @model_validator(mode="before")
    @classmethod
    def _derive(cls, data: Any) -> Any:
        if isinstance(data, dict):
            kind = data.get("error_kind", "")
            limit = data.get("limit")
            it = data.get("iteration_count")
            data.setdefault("headline", f"max iterations: {it}/{limit}")
            data.setdefault(
                "details",
                {
                    "kind": str(kind),
                    "limit": str(limit),
                    "iteration": str(it),
                },
            )
            data.setdefault("body", str(data.get("message", "")))
        return data


class PersistenceFailed(TerminalEvent):
    """Не удалось прочитать/записать journal/хранилище."""

    type: Literal["PersistenceFailed"] = "PersistenceFailed"
    message: str

    @model_validator(mode="before")
    @classmethod
    def _derive(cls, data: Any) -> Any:
        if isinstance(data, dict):
            kind = data.get("error_kind", "")
            data.setdefault("headline", f"persistence failed: {kind}")
            data.setdefault("details", {"kind": str(kind)})
            data.setdefault("body", str(data.get("message", "")))
        return data


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
    | LLMRequestSent
    | LLMResponseStreamOpened
    | GenerationStarted
    | ThinkingStarted
    | AnswerStarted
    | ToolCallStreamStarted
    | ToolExecutionStarted
    | GenerationRetried
    | GenerationDone
    # ContentDeltaEvent
    | ThinkingToken
    | AnswerToken
    | RefusalToken
    | ToolCallArgumentDelta
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
)


AgentEventName: TypeAlias = Literal[
    "IterationStarted",
    "LLMRequestSent",
    "LLMResponseStreamOpened",
    "GenerationStarted",
    "ThinkingStarted",
    "AnswerStarted",
    "ToolCallStreamStarted",
    "ToolExecutionStarted",
    "GenerationRetried",
    "GenerationDone",
    "ThinkingToken",
    "AnswerToken",
    "RefusalToken",
    "ToolCallArgumentDelta",
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
            msg = "AgentEventRegistry.decode: отсутствует поле 'type'"
            raise ValueError(msg)
        event_cls = cls._by_type.get(type_value)
        if event_cls is None:
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
        return event_cls.model_validate(data)


def _register_core_events() -> None:
    for cls in (
        IterationStarted,
        LLMRequestSent,
        LLMResponseStreamOpened,
        GenerationStarted,
        ThinkingStarted,
        AnswerStarted,
        ToolCallStreamStarted,
        ToolExecutionStarted,
        GenerationRetried,
        GenerationDone,
        ThinkingToken,
        AnswerToken,
        RefusalToken,
        ToolCallArgumentDelta,
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
    ):
        AgentEventRegistry.register(cls)


_register_core_events()


def _verify_agent_event_names_exhaustive(e: AgentEvent) -> AgentEventName:
    """Compile-time гарантия синхронности union и Literal-имён."""
    return e.type  # type: ignore[return-value]
