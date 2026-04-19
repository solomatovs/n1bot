"""Централизованная маршрутизация :class:`RoutableError` в события и
побочные эффекты + верхнеуровневый try/except."""

from __future__ import annotations

from collections.abc import Iterable, Iterator

from boba.domain.agent.errors import (
    LLMError,
    LLMToolCallFormatError,
    MaxIterationsExceededError,
    RepeatedFormatFailureError,
    ToolFeedbackError,
)
from boba.domain.agent.events import (
    AgentEvent,
    GenerationFailed,
    MaxIterationsReached,
    PersistenceFailed,
    PromptFailed,
    RepeatedFormatFailure,
    ToolCallFormatFailed,
    ToolExecutionFailed,
    UserNoticeReady,
)
from boba.domain.agent.history import HistoryError
from boba.domain.agent.messages import MessageService
from boba.domain.agent.models import AgentContext, LLMMessage
from boba.domain.agent.prompt import PromptError
from boba.domain.core.errors import (
    LLMFeedbackError,
    Retryable,
    RoutableError,
    UserNoticeError,
)
from boba.domain.core.patterns import StreamSource


class AgentErrorRouter:
    """Централизованная маршрутизация :class:`RoutableError` в события и
    побочные эффекты.

    Единая точка знания «какой тип ошибки → какое событие + какой сайд-
    эффект». Middleware, поймавший :class:`RoutableError`, делегирует сюда.

    Две API-точки для клиентов:

    - :meth:`route` — разобрать одну ошибку (вызывается верхнеуровневым
      :class:`AgentErrorRouterMiddleware` из top-level try/except).
    - :meth:`run_batch` — прогнать серию подзадач-генераторов с
      автоматическим сбором :class:`LLMFeedbackError` и маршрутизацией
      в конце. Batch-middleware (напр. :class:`ToolExecutionMiddleware`)
      **не пишет** свой try/except — просто передают генераторы сюда.

    Маршруты (match-case):

    - :class:`LLMError` → :class:`GenerationFailed` (``status_code``,
      ``retryable`` по маркеру :class:`Retryable`). Терминально.
    - :class:`PromptError` → :class:`PromptFailed` (``provider``).
      Терминально.
    - :class:`HistoryError` → :class:`PersistenceFailed`. Терминально.
    - :class:`MaxIterationsExceededError` → :class:`MaxIterationsReached`.
      Терминально.
    - :class:`RepeatedFormatFailureError` → :class:`RepeatedFormatFailure`.
      Терминально.
    - :class:`ToolFeedbackError` → запись ``LLMMessage(role="tool",
      tool_call_id=..., content=...)`` в :class:`MessageService` +
      :class:`ToolExecutionFailed`. Не терминально.
    - :class:`LLMToolCallFormatError` → запись ``LLMMessage(role="user",
      content=...)`` в :class:`MessageService` +
      :class:`ToolCallFormatFailed`. Не терминально.
    - :class:`UserNoticeError` → :class:`UserNoticeReady`. Не терминально.
    - Непокрытый подкласс → :class:`TypeError` (баг: забыли добавить
      ветку).
    """

    def __init__(self, message_service: MessageService) -> None:
        self._message_service = message_service

    def route(self, ctx: AgentContext, err: RoutableError) -> Iterator[AgentEvent]:
        rid = ctx.request.request_id
        retryable = isinstance(err, Retryable)
        kind = type(err).__name__
        msg = str(err)
        match err:
            case LLMError():
                yield GenerationFailed(
                    request_id=rid,
                    error_kind=kind,
                    message=msg,
                    retryable=retryable,
                    status_code=err.status_code,
                )
            case PromptError():
                yield PromptFailed(
                    request_id=rid,
                    error_kind=kind,
                    message=msg,
                    retryable=retryable,
                    provider=err.provider,
                )
            case HistoryError():
                yield PersistenceFailed(
                    request_id=rid,
                    error_kind=kind,
                    message=msg,
                    retryable=retryable,
                )
            case MaxIterationsExceededError():
                yield MaxIterationsReached(
                    request_id=rid,
                    error_kind=kind,
                    message=msg,
                    limit=err.limit,
                    iteration=err.iteration,
                )
            case RepeatedFormatFailureError():
                yield RepeatedFormatFailure(
                    request_id=rid,
                    error_kind=kind,
                    message=msg,
                    count=err.count,
                    limit=err.limit,
                )
            case ToolFeedbackError():
                self._message_service.add(
                    LLMMessage(
                        role="tool",
                        content=err.message,
                        tool_call_id=err.tool_call_id,
                    ),
                )
                yield ToolExecutionFailed(
                    request_id=rid,
                    tool_call_id=err.tool_call_id,
                    tool_name=err.tool_name,
                    error_kind=err.error_kind,
                    message=err.message,
                )
            case LLMToolCallFormatError():
                self._message_service.add(
                    LLMMessage(
                        role="user",
                        content=err.message,
                    ),
                )
                yield ToolCallFormatFailed(
                    request_id=rid,
                    error_kind=kind,
                    message=err.message,
                )
            case UserNoticeError():
                yield UserNoticeReady(
                    request_id=rid,
                    message=err.message,
                    severity=err.severity,
                )
            case _:
                raise TypeError(
                    f"AgentErrorRouter: unmapped RoutableError subclass "
                    f"{kind!r}. Добавь ветку в route или унаследуй новый тип "
                    f"от одного из известных семейств."
                ) from err

    def run_batch(
        self,
        ctx: AgentContext,
        tasks: Iterable[Iterator[AgentEvent]],
    ) -> Iterator[AgentEvent]:
        """Прогоняет серию подзадач-генераторов с batch-семантикой для
        :class:`LLMFeedbackError`.

        События успешных подзадач стримятся сразу (``yield from``).
        Ошибка уровня :class:`LLMFeedbackError` из любой подзадачи
        **не обрывает** остальные: ловится, копится, маршрутизируется
        после завершения всей серии (в порядке возникновения).

        :class:`TerminalError` (и любое не-``LLMFeedbackError``
        ``AgentError``) — пропускаем наверх немедленно: при сбое LLM /
        persistence дальше работать смысла нет, batch прерывается.

        API задуман так, чтобы middleware-автор не писал свой
        ``try/except`` и не думал про batch — просто передаёт
        генераторы подзадач::

            yield from self._error_router.run_batch(
                ctx, (self._run_task(item) for item in items)
            )
        """
        deferred: list[LLMFeedbackError] = []
        for task in tasks:
            try:
                yield from task
            except LLMFeedbackError as e:
                deferred.append(e)
        for err in deferred:
            yield from self.route(ctx, err)


class AgentErrorRouterMiddleware(StreamSource[AgentContext, AgentEvent]):
    """Верхнеуровневый try/except над всем source-chain'ом.

    Ставится **самым внешним** слоем (поверх retry, промптов, history,
    tool-execution). Любой middleware глубже может ``raise`` подкласс
    :class:`RoutableError`, не зная, что будет дальше — средний слой
    прокидывает, этот ловит и делегирует :class:`AgentErrorRouter`.

    Замечание про batch-middleware: они ловят :class:`LLMFeedbackError`
    **сами** (per-iteration) и вызывают :meth:`AgentErrorRouter.route`
    напрямую — чтобы одна упавшая подзадача не оборвала остальные.
    Досюда из батч-middleware долетают только терминальные ошибки.

    Всё, что не наследует :class:`RoutableError` (``KeyError``, ``TypeError``
    и т.п.), проходит насквозь и крашит процесс — баги не маскируем.
    :class:`Retryable`-подклассы сюда доезжают только после исчерпания
    попыток во внутреннем retry-слое.
    """

    def __init__(
        self,
        inner: StreamSource[AgentContext, AgentEvent],
        router: AgentErrorRouter,
    ) -> None:
        self._inner = inner
        self._router = router

    def name(self) -> str:
        return "AgentErrorRouter"

    def stream(self, ctx: AgentContext) -> Iterator[AgentEvent]:
        try:
            yield from self._inner.stream(ctx)
        except RoutableError as e:
            yield from self._router.route(ctx, e)
