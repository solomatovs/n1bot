"""Полиморфная маршрутизация :class:`RoutableError` и верхнеуровневый
try/except агентской цепочки.

:class:`AgentErrorRouter` работает через полиморфизм: знает только
о семействах (:class:`UserFeedbackError` / :class:`LLMFeedbackError`)
и делегирует построение события/фидбека самой ошибке. Добавление
нового concrete-типа не требует правок роутера — достаточно
унаследоваться от соответствующего binding-бейза и реализовать
``to_*_event`` / ``to_llm_feedback``.

Две API-точки:

- :meth:`AgentErrorRouter.route` — разобрать **одну** ошибку. Обычно
  вызывается из :class:`AgentErrorRouterMiddleware` (top-level
  try/except) или напрямую batch-middleware, который хочет
  маршрутизировать отложенные ошибки по ходу собственной логики.
- :meth:`AgentErrorRouter.run_batch` — прогнать серию
  под-генераторов с batch-семантикой: успешные события стримятся
  сразу, :class:`LLMFeedbackError` **не обрывают** остальные
  подзадачи — ловятся, копятся, маршрутизируются после. Это — про
  сценарии вроде «исполнение нескольких параллельных tool_calls за
  одну итерацию: если один упал, остальные должны отработать».

:class:`AgentErrorRouterMiddleware` ставится **самым внешним** слоем
агент-цепочки — поверх loop, retry и всего остального. Любой
middleware глубже может ``raise`` подкласс :class:`RoutableError`,
не зная, что будет дальше — средний слой пропускает, этот ловит и
делегирует роутеру. Не-:class:`RoutableError` исключения проходят
насквозь и крашат процесс (баги не маскируем).
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator

from boba.domain.core.errors import (
    LLMFeedbackError,
    RoutableError,
    UserFeedbackError,
)
from boba.domain.core.patterns import StreamSource
from boba_2.domain.agent.events import AgentEvent
from boba_2.domain.agent.messages import MessageService
from boba_2.domain.agent.models import AgentContext


class AgentErrorRouter:
    """Полиморфная маршрутизация :class:`RoutableError` через
    ``to_*_event`` / ``to_llm_feedback``.
    """

    def __init__(self, message_service: MessageService) -> None:
        self._message_service = message_service

    def route(
        self, ctx: AgentContext, err: RoutableError,
    ) -> Iterator[AgentEvent]:
        """Разобрать одну ошибку в поток событий + побочный эффект.

        Контракт:

        - :class:`LLMFeedbackError` → пишет ``to_llm_feedback()`` в
          :class:`MessageService`, затем эмитит ``to_user_event()``
          в поток.
        - :class:`UserFeedbackError` (не LLM) → только
          ``to_user_event()`` в поток.
        - Любой :class:`RoutableError`, не являющийся
          :class:`UserFeedbackError`, — :class:`TypeError`
          (мисконфиг иерархии: добавили ошибку, не привязав к
          семье).
        """
        rid = ctx.agent_request.request_id
        if not isinstance(err, UserFeedbackError):
            raise TypeError(
                f"{type(err).__name__}: RoutableError, но не унаследован от "
                "UserFeedbackError. Добавь наследование от одного из "
                "binding-бейзов (AgentTerminalError / AgentLLMFeedbackError "
                "/ AgentUserNotice).",
            ) from err
        if isinstance(err, LLMFeedbackError):
            self._message_service.add(err.to_llm_feedback())
        yield err.to_user_event(rid)

    def run_batch(
        self,
        ctx: AgentContext,
        tasks: Iterable[Iterator[AgentEvent]],
    ) -> Iterator[AgentEvent]:
        """Прогнать серию подзадач с batch-семантикой для
        :class:`LLMFeedbackError`.

        События успешных подзадач стримятся по мере поступления
        (``yield from``). Ошибка :class:`LLMFeedbackError` из любой
        подзадачи **не обрывает** остальные: ловится, копится,
        маршрутизируется в конце в порядке возникновения.

        :class:`TerminalError` (и любое не-:class:`LLMFeedbackError`
        ``RoutableError``) проходит наверх немедленно — при
        терминальном сбое дальше работать бессмысленно, batch
        обрывается.

        API рассчитан на middleware, которой не хочется писать свой
        ``try/except`` и думать про batch. Пример::

            yield from self._error_router.run_batch(
                ctx, (self._run_tool(tc) for tc in pending)
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
    """Верхнеуровневый try/except над всей агентской цепочкой.

    Ставится **самым внешним** слоем — поверх
    ``SystemPrompt``/``UserPrompt``, loop, всего остального. Любой
    нижележащий middleware может бросить :class:`RoutableError`, не
    зная что дальше.

    Замечания:

    - Batch-middleware (например, :class:`ToolExecutionMiddleware`)
      ловят :class:`LLMFeedbackError` **сами** per-iteration через
      :meth:`AgentErrorRouter.run_batch` — чтобы одна упавшая
      подзадача не обрывала остальные. До верхнего boundary
      долетают только терминальные ошибки.
    - Не-:class:`RoutableError` исключения проходят насквозь —
      крашат процесс. :class:`Retryable`-подклассы доезжают сюда
      только после исчерпания попыток в retry-слое.
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

    def reset(self) -> None:
        self._inner.reset()

    def stream(self, ctx: AgentContext) -> Iterable[AgentEvent]:
        try:
            yield from self._inner.stream(ctx)
        except RoutableError as e:
            yield from self._router.route(ctx, e)
