"""Централизованная маршрутизация :class:`RoutableError` в события и
побочные эффекты + верхнеуровневый try/except."""

from __future__ import annotations

from collections.abc import Iterable, Iterator

from boba.domain.agent.events import AgentEvent
from boba.domain.agent.messages import MessageService
from boba.domain.agent.models import AgentContext
from boba.domain.core.errors import (
    LLMFeedbackError,
    RoutableError,
    UserFeedbackError,
)
from boba.domain.core.patterns import StreamSource


class AgentErrorRouter:
    """Полиморфная маршрутизация :class:`RoutableError` через ``to_*_event``.

    Роутер не знает конкретных подклассов — он смотрит только на
    семейство (:class:`UserFeedbackError` / :class:`LLMFeedbackError`) и
    делегирует построение события самой ошибке. Добавление новой ошибки
    не требует правок роутера: достаточно унаследоваться от
    соответствующего binding-бейза (:class:`AgentTerminalError` /
    :class:`AgentLLMFeedbackError` / :class:`AgentUserNotice`) и
    реализовать abstract-методы.

    Две API-точки для клиентов:

    - :meth:`route` — разобрать одну ошибку (вызывается верхнеуровневым
      :class:`AgentErrorRouterMiddleware` из top-level try/except).
    - :meth:`run_batch` — прогнать серию подзадач-генераторов с
      автоматическим сбором :class:`LLMFeedbackError` и маршрутизацией
      в конце.

    Маршруты:

    - :class:`UserFeedbackError` → ``err.to_user_event()`` в поток
      (event для sink'ов). Терминальность определяется подклассом —
      :class:`TerminalError` остановит цикл через
      :class:`StopOnAnyFailure`, :class:`UserNoticeError` /
      :class:`LLMFeedbackError` — нет.
    - :class:`LLMFeedbackError` (подкласс ``UserFeedbackError``) →
      дополнительно ``err.to_llm_feedback()`` в :class:`MessageService`.
    - :class:`RoutableError` без принадлежности к
      :class:`UserFeedbackError` → :class:`TypeError` (мисконфиг
      иерархии).
    """

    def __init__(self, message_service: MessageService) -> None:
        self._message_service = message_service

    def route(self, ctx: AgentContext, err: RoutableError) -> Iterator[AgentEvent]:
        rid = ctx.agent_request.request_id
        if not isinstance(err, UserFeedbackError):
            raise TypeError(
                f"{type(err).__name__}: RoutableError, но не унаследован от "
                "UserFeedbackError. Наследуй от семейства.",
            ) from err
        if isinstance(err, LLMFeedbackError):
            self._message_service.add(err.to_llm_feedback())
        yield err.to_user_event(rid)

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
        ``RoutableError``) — пропускаем наверх немедленно: при сбое LLM /
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
