"""Agent-специфичные доменные ошибки + практический гайд по всей иерархии.

Этот модуль держит LLM-иерархию (:class:`LLMError` + подклассы),
:class:`ToolFeedbackError`, :class:`LLMToolCallFormatError` и
лимит-ошибки цикла. Базовые маркеры (:class:`RoutableError`,
:class:`TerminalError`, :class:`UserFeedbackError`,
:class:`LLMFeedbackError`, :class:`Retryable`) живут в
:mod:`boba.domain.core.errors`.

Каждая concrete-ошибка явно объявляет свои эффекты миксами маркеров
с прямой привязкой generics:
``UserFeedbackError[RequestId, AgentEvent]``,
``LLMFeedbackError[LLMMessage]``.


════════════════════════════════════════════════════════════════════
  Полная иерархия
════════════════════════════════════════════════════════════════════

::

    RoutableError                                          [core.errors]
    │
    ├── UserFeedbackError[TReqId, TUserEvent]    event для sink
    │       to_event(request_id) -> TUserEvent
    │
    ├── LLMFeedbackError[TFeedback]              фидбек для LLM
    │       to_llm_feedback() -> TFeedback
    │
    ├── TerminalError                            маркер остановки цикла
    └── Retryable                                маркер повтора

Concrete-ошибки агент-слоя (миксы):

- ``UserFeedbackError[RequestId, AgentEvent] + TerminalError``:
  :class:`LLMError` (+ подклассы: :class:`RetryableLLMError`,
  :class:`PermanentLLMError` и т.д.), :class:`PromptError`
  (``agent.prompt``), :class:`MessageStoreError` (``agent.messages``),
  :class:`MaxIterationsExceededError`, :class:`RepeatedFormatFailureError`.
- ``UserFeedbackError[RequestId, AgentEvent] + LLMFeedbackError[LLMMessage]``:
  :class:`ToolFeedbackError`, :class:`LLMToolCallFormatError`.
- ``UserFeedbackError[RequestId, AgentEvent]`` (без terminal):
  :class:`AgentUserNotice` (+ severity).


════════════════════════════════════════════════════════════════════
  Сценарий → что бросать (quick reference)
════════════════════════════════════════════════════════════════════

┌──────────────────────────────────┬────────────────────────────────┐
│ Сценарий                         │ Что бросать                    │
├──────────────────────────────────┼────────────────────────────────┤
│ LLM упала/недоступна             │ LLMError / подкласс            │
│ Промпт не собрался               │ PromptError (agent.prompt)     │
│ Хранилище сообщений недоступно   │ MessageStoreError (agent.msg)  │
│ Tool упал (LLM должна увидеть)   │ ToolFeedbackError              │
│ LLM сломала формат tool call     │ LLMToolCallFormatError         │
│ Нотис пользователю               │ AgentUserNotice                │
│ Исчерпан лимит итераций цикла    │ MaxIterationsExceededError     │
│ Модель залипла на сбое формата   │ RepeatedFormatFailureError     │
│ Добавить авто-повтор             │ + миксин Retryable             │
└──────────────────────────────────┴────────────────────────────────┘


════════════════════════════════════════════════════════════════════
  Примеры
════════════════════════════════════════════════════════════════════

**Остановить цикл**::

    from boba.domain.agent.errors import LLMContextLengthError
    raise LLMContextLengthError("context window exceeded", status_code=400)

    from boba.domain.agent.prompt import PromptProviderError
    try:
        data = read_source(...)
    except OSError as e:
        raise PromptProviderError(f"cannot read: {e}", provider="git") from e

**Сказать LLM про ошибку tool**::

    from boba.domain.agent.errors import ToolFeedbackError
    raise ToolFeedbackError(
        tool_call_id=tc.tool_call_id,
        tool_name=tc.tool_name,
        error_kind="ToolExecutionError",
        message="file not found: foo.txt",
    )

**Показать нотис пользователю**::

    from boba.domain.agent.errors import AgentUserNotice
    raise AgentUserNotice(
        "Выбранная модель устарела, переключитесь на новую.",
        severity="warning",  # "info" | "warning" | "error"
    )

**Сделать ошибку повторяемой** — миксин ``Retryable``::

    from boba.domain.core.errors import Retryable
    from boba.domain.agent.prompt import PromptError

    class PromptLockFileError(PromptError, Retryable):
        '''Файл временно залочен — повторим.'''


════════════════════════════════════════════════════════════════════
  Как добавить новый тип ошибки
════════════════════════════════════════════════════════════════════

1. Выбери маркеры-эффекты (любая комбинация):

   - :class:`UserFeedbackError[RequestId, AgentEvent]` — event для sink
     (обязателен ``to_event``);
   - :class:`LLMFeedbackError[LLMMessage]` — фидбек для LLM
     (обязателен ``to_llm_feedback``);
   - :class:`TerminalError` — остановка цикла;
   - :class:`Retryable` — повтор в retry-middleware.

2. Объяви класс с прямым миксом::

    class Foo(
        UserFeedbackError[RequestId, AgentEvent],
        TerminalError,
    ):
        def to_event(self, rid): ...

3. Добавь поля контекста в ``__init__``.
4. Если нужно новое событие — добавь dataclass в ``events.py``,
   занеси в ``AgentEvent``-union и ``AgentEventName``-literal.
5. Обнови sink'и (``ConsoleSink`` / ``HistorySink``) при необходимости.

Роутер (:class:`~boba.domain.agent.meat.error_routing.AgentErrorRouter`)
**не правим** — он работает через полиморфизм по маркерам. Забытый
``to_*`` метод ловится ruff/pyright-ом статически через
``@abstractmethod``; в рантайме первый же вызов поднимает
:class:`NotImplementedError`.


════════════════════════════════════════════════════════════════════
  Что НЕ должно быть RoutableError
════════════════════════════════════════════════════════════════════

Программные баги (``KeyError``, ``AttributeError``, ``TypeError``,
``ValueError`` на boundary-нарушениях) — **пусть летят мимо** и крашат
процесс. Маскировать баги доменными ошибками запрещено — иначе баг
ляжет в UI или LLM как легитимный сигнал.

При переупаковке всегда сохраняй причину: ``raise NewError(...) from e``.
"""

from __future__ import annotations

from boba.domain.agent.events import (
    AgentEvent,
    GenerationFailed,
    MaxIterationsReached,
    RepeatedFormatFailure,
    ToolCallFormatFailed,
    ToolExecutionFailed,
    UserNoticeReady,
    UserNoticeSeverity,
)
from boba.domain.agent.models import LLMMessage, RequestId
from boba.domain.core.errors import (
    LLMFeedbackError,
    Retryable,
    TerminalError,
    UserFeedbackError,
)


# ═════════════════════════════════════════════════════════════════════
#  Concrete: user notice (цикл идёт, severity)
# ═════════════════════════════════════════════════════════════════════


class AgentUserNotice(UserFeedbackError[RequestId, AgentEvent]):
    """Нотис пользователю с ``severity``. Цикл не прерывается.

    Роутер эмитит :class:`~boba.domain.agent.events.UserNoticeReady`.
    """

    def __init__(
        self,
        message: str,
        *,
        severity: UserNoticeSeverity = "info",
    ) -> None:
        super().__init__(message)
        self.message = message
        self.severity: UserNoticeSeverity = severity

    def to_event(self, request_id: RequestId) -> AgentEvent:
        return UserNoticeReady(
            request_id=request_id,
            message=self.message,
            severity=self.severity,
        )


# ═════════════════════════════════════════════════════════════════════
#  LLM-иерархия
# ═════════════════════════════════════════════════════════════════════


class LLMError(UserFeedbackError[RequestId, AgentEvent], TerminalError):
    """Базовая доменная ошибка обращения к LLM.

    Адаптеры провайдеров (OpenAI, Ollama, …) ловят сырые исключения
    (``openai.*``, ``httpx.*``) и конвертируют их в потомков этого типа.
    Выше по стеку код работает только с доменными типами.
    """

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code

    def to_event(self, request_id: RequestId) -> AgentEvent:
        return GenerationFailed(
            request_id=request_id,
            error_kind=type(self).__name__,
            message=str(self),
            retryable=isinstance(self, Retryable),
            status_code=self.status_code,
        )


class RetryableLLMError(LLMError, Retryable):
    """Ошибка LLM, которую имеет смысл повторить: сеть, таймаут, rate-limit, 5xx."""


class PermanentLLMError(LLMError):
    """Ошибка LLM, которую повторять бессмысленно: auth, bad request, context length."""


class LLMConnectionError(RetryableLLMError):
    """Не удалось установить соединение с провайдером (DNS, refused, reset)."""


class LLMTimeoutError(RetryableLLMError):
    """Превышен таймаут запроса/чтения ответа."""


class LLMRateLimitError(RetryableLLMError):
    """Провайдер ответил 429 Too Many Requests."""


class LLMProviderInternalError(RetryableLLMError):
    """Провайдер ответил 5xx или оборвал стрим по внутренней причине."""


class LLMAuthError(PermanentLLMError):
    """Провайдер ответил 401/403 — неверный или отозванный ключ."""


class LLMInvalidRequestError(PermanentLLMError):
    """Провайдер ответил 400 — запрос сформирован некорректно."""


class LLMContextLengthError(PermanentLLMError):
    """Суммарная длина сообщений превысила окно модели."""


class LLMProtocolError(PermanentLLMError):
    """Провайдер вернул ответ вне ожидаемой схемы.

    Пример: ``finish_reason`` пришёл со значением, которого нет в
    :class:`~boba.domain.agent.events.FinishReason`. Retry не поможет —
    провайдер вернёт ту же структуру.
    """


class LLMRequestModelNoneError(LLMInvalidRequestError):
    """LLM-запрос собран без ``model``.

    Специализация :class:`LLMInvalidRequestError`: роутер автоматически
    превратит в :class:`GenerationFailed`.
    """

    def __init__(self) -> None:
        super().__init__("LLMRequest.model is None")


# ═════════════════════════════════════════════════════════════════════
#  Лимиты цикла
# ═════════════════════════════════════════════════════════════════════


class MaxIterationsExceededError(
    UserFeedbackError[RequestId, AgentEvent],
    TerminalError,
):
    """Агентский цикл исчерпал лимит итераций без финального ответа.

    Поднимается :class:`~boba.domain.agent.meat.IterationCounterMiddleware`,
    когда ``ctx.iteration`` превысил ``ctx.config.max_iterations``.
    """

    def __init__(self, message: str, *, limit: int, iteration: int) -> None:
        super().__init__(message)
        self.limit = limit
        self.iteration = iteration

    def to_event(self, request_id: RequestId) -> AgentEvent:
        return MaxIterationsReached(
            request_id=request_id,
            error_kind=type(self).__name__,
            message=str(self),
            limit=self.limit,
            iteration=self.iteration,
        )


class RepeatedFormatFailureError(
    UserFeedbackError[RequestId, AgentEvent],
    TerminalError,
):
    """Модель N раз подряд вывела неверный формат tool call.

    Поднимается
    :class:`~boba.domain.agent.meat.RepeatedFormatFailureGuardMiddleware`
    после накопления ``limit`` подряд
    :class:`~boba.domain.agent.events.ToolCallFormatFailed`.
    """

    def __init__(self, message: str, *, count: int, limit: int) -> None:
        super().__init__(message)
        self.count = count
        self.limit = limit

    def to_event(self, request_id: RequestId) -> AgentEvent:
        return RepeatedFormatFailure(
            request_id=request_id,
            error_kind=type(self).__name__,
            message=str(self),
            count=self.count,
            limit=self.limit,
        )


# ═════════════════════════════════════════════════════════════════════
#  LLM feedback
# ═════════════════════════════════════════════════════════════════════


class ToolFeedbackError(
    UserFeedbackError[RequestId, AgentEvent],
    LLMFeedbackError[LLMMessage],
):
    """Ошибка выполнения конкретного tool'а.

    Роутер пишет ``LLMMessage`` с ``role="tool"``,
    ``tool_call_id=<tool_call_id>``, ``content=<message>`` и эмитит
    :class:`~boba.domain.agent.events.ToolExecutionFailed`.

    Поднимается ``ToolExecutionMiddleware``-ом (обогащает «сырую»
    :class:`~boba.domain.core.tools.ToolExecutionError` из
    :class:`~boba.domain.core.tools.ToolsService` идентификатором
    tool call'а).
    """

    def __init__(
        self,
        *,
        tool_call_id: str,
        tool_name: str,
        error_kind: str,
        message: str,
    ) -> None:
        super().__init__(message)
        self.tool_call_id = tool_call_id
        self.tool_name = tool_name
        self.error_kind = error_kind
        self.message = message

    def to_event(self, request_id: RequestId) -> AgentEvent:
        return ToolExecutionFailed(
            request_id=request_id,
            tool_call_id=self.tool_call_id,
            tool_name=self.tool_name,
            error_kind=self.error_kind,
            message=self.message,
        )

    def to_llm_feedback(self) -> LLMMessage:
        return LLMMessage(
            role="tool",
            content=self.message,
            tool_call_id=self.tool_call_id,
        )


class LLMToolCallFormatError(
    UserFeedbackError[RequestId, AgentEvent],
    LLMFeedbackError[LLMMessage],
):
    """LLM нарушила формат content-as-JSON tool call'а.

    Роутер пишет ``LLMMessage`` с ``role="user"``, ``content=<message>``
    в :class:`MessageService` и эмитит
    :class:`~boba.domain.agent.events.ToolCallFormatFailed`. Цикл не
    прерывается — LLM на следующей итерации увидит критику своего
    предыдущего вывода и сможет переформулировать tool call.

    Поднимается парсером content-as-JSON
    (:class:`~boba.domain.agent.meat.StrictJsonToolCallParser`), когда
    LLM эмитит JSON-объект с неверной структурой (невалидный JSON,
    не-объект на корне, отсутствующие/посторонние поля, неверные типы).

    ``message`` уже включает цитату сырого content'а и описание, что
    именно не так — LLM получает самодостаточный фидбек.
    """

    def __init__(
        self,
        message: str,
        *,
        raw_content: str,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.raw_content = raw_content

    def to_event(self, request_id: RequestId) -> AgentEvent:
        return ToolCallFormatFailed(
            request_id=request_id,
            error_kind=type(self).__name__,
            message=self.message,
        )

    def to_llm_feedback(self) -> LLMMessage:
        return LLMMessage(
            role="user",
            content=self.message,
        )
