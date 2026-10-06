"""Слушатели обращений к модели одного хода: AgentTracer публикует рассуждения,
расход токенов и этап подготовки в шину через TurnFeed, LlmStateLog пишет
смену состояний прогона в журнал. События приходят типами диалога чата от
агента сессии; вызовы инструментов идут мимо модели и сюда не приходят.

Ошибки: своих не выпускает; сбой публикации показывается в чат и журнал одним
разбором FailureReport, сбой журналирования уходит тому, кто раздаёт события.
"""

from __future__ import annotations

import functools
import logging
import time
from abc import abstractmethod
from collections.abc import Callable, Coroutine, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, ClassVar, Protocol, TypeVar

from typing_extensions import ParamSpec

from boba.chainlit.chat.dialog import (
    AssistantMessage,
    ModelCallFinished,
    ModelCallStarted,
    ModelListener,
    ModelToken,
    PrefetchStage,
)
from boba.chainlit.chat.feed import TurnFeed
from boba.chainlit.rendering.chat_view import StepText
from boba.chainlit.rendering.errors import show_error
from boba.identity.errors import FailureReport
from boba.identity.session import LogUserMark
from chainlit.context import context_var

__all__ = [
    "AgentTracer",
    "LlmStage",
    "LlmStageEvent",
    "LlmStateLog",
    "TurnArtifacts",
]

logger = logging.getLogger(__name__)


_P = ParamSpec("_P")
_R = TypeVar("_R")


def _visible_failure(
    fn: Callable[_P, Coroutine[Any, Any, _R]],
) -> Callable[_P, Coroutine[Any, Any, _R | None]]:
    """Делает сбой слушателя видимым: колбэк-менеджер графа гасит исключения
    колбэков в logger.warning."""

    @functools.wraps(fn)
    async def wrapper(*args: _P.args, **kwargs: _P.kwargs) -> _R | None:
        try:
            return await fn(*args, **kwargs)
        except Exception as e:
            report = FailureReport.of(e)
            logger.exception("rendering failed in %s: %s", fn.__name__, report.log)
            if report.view:
                shown = f"Failed to render step ({fn.__name__}): {report.view}"
                await show_error(shown)
            return None

    return wrapper


class TurnArtifacts(Protocol):
    """Порт незавершённых артефактов хода, которые ведёт трасер: потоковые
    рассуждения прогонов модели.
    """

    @abstractmethod
    def add_reasoning(self, run_key: str, text: str) -> None: ...

    @abstractmethod
    def take_reasoning(self, run_key: str) -> str: ...


class AgentTracer(ModelListener, PrefetchStage):
    """Трасер ленты одного хода: публикует в шину рассуждения модели, расход
    токенов и этап подготовки хода.

    Реализация ModelListener и PrefetchStage. Создаёт его ход (ChatTurn) и
    отдаёт агенту сессии вместе с вопросом; события прогонов модели и этапа
    подготовки приходят от графа хода. Вызовы инструментов сюда не
    приходят: их шаги ведёт путь вызова узла (ChatCalls).
    """

    def __init__(self, feed: TurnFeed, state: TurnArtifacts) -> None:
        self._context = context_var.get()
        self._feed = feed
        self._state = state

    @staticmethod
    def _key_of(message_id: str | None, run: str) -> str:
        """Ключ шага рассуждений: id сообщения модели, а без него — id прогона."""
        if message_id:
            return message_id

        return run

    def _set_context(self) -> None:
        context_var.set(self._context)

    async def model_called(self, call: ModelCallStarted) -> None:
        return None

    @_visible_failure
    async def model_token(self, token: ModelToken) -> None:
        self._set_context()

        if not token.reasoning:
            return

        self._state.add_reasoning(token.run, token.reasoning)
        await self._feed.thinking_token(
            self._key_of(token.message_id, token.run), token.reasoning
        )

    @_visible_failure
    async def model_replied(self, finish: ModelCallFinished) -> None:
        self._set_context()

        streamed = self._state.take_reasoning(finish.run)
        await self._feed.thinking_closed()

        reply = finish.reply
        if reply is None:
            return

        await self._spend_tokens(reply, finish.run)

        # рассуждения без стрима приходят разом в итоговом сообщении
        if streamed:
            return

        if not reply.reasoning:
            return

        await self._feed.thinking_complete(
            self._key_of(reply.id, finish.run), reply.reasoning
        )

    async def _spend_tokens(self, reply: AssistantMessage, run: str) -> None:
        """Публикует расход прогона; без учёта от провайдера публиковать нечего."""
        usage = reply.usage
        if not usage.counted:
            return

        await self._feed.tokens_spent(
            self._key_of(reply.id, run),
            usage.input_tokens,
            usage.output_tokens,
            usage.reasoning_tokens,
        )

    @_visible_failure
    async def model_failed(self, run: str, error: BaseException) -> None:
        self._set_context()

        await self._feed.thinking_closed()

    async def begin(self) -> None:
        await self._feed.stage_started(
            StepText.PREFETCH.value, StepText.REPHRASING.value
        )

    async def searching(self, queries: Sequence[str]) -> None:
        await self._feed.stage_queries(StepText.PREFETCH.value, queries)

    async def end(self, queries: Sequence[str], elapsed_ms: int) -> None:
        await self._feed.stage_ended(StepText.PREFETCH.value, queries, elapsed_ms)


class LlmStage(StrEnum):
    """Состояния, которые проходит один прогон модели: запрос, рассуждение, ответ."""

    REQUEST = "request"
    THINKING = "thinking"
    ANSWER = "answer"


class LlmStageEvent(StrEnum):
    """Что случилось со стадией прогона: началась, закончилась, упала или пришла
    целиком.
    """

    STARTED = "started"
    FINISHED = "finished"
    FAILED = "failed"
    COMPLETE = "complete"
    """Стадия пришла разом, без стрима: у неё нет начала и конца во времени."""


@dataclass
class StageProgress:
    """Идущая стадия прогона: чем занят провайдер и сколько символов уже отдал."""

    stage: LlmStage
    started: float
    chars: int = 0

    def add(self, text: str) -> None:
        self.chars += len(text)

    def elapsed_ms(self, now: float) -> int:
        return int((now - self.started) * 1000)


@dataclass
class RunProgress:
    """Один прогон модели от отправки запроса до финального сообщения: время старта,
    первый токен и текущая стадия.
    """

    LABEL_LEN: ClassVar[int] = 8

    run_id: str
    started: float
    first_token: float = 0.0
    """Момент первого токена; 0 — провайдер ещё ничего не прислал."""

    stage: StageProgress | None = None

    @property
    def label(self) -> str:
        return self.run_id[: self.LABEL_LEN]

    def elapsed_ms(self, now: float) -> int:
        return int((now - self.started) * 1000)


class LlmStateLog(ModelListener):
    """Журнал состояний обмена с провайдером: пишет в лог каждую смену
    состояния прогона модели и длительность стадий.

    Реализация ModelListener. Создаётся обработчиком сообщения на ход с
    меткой пользователя и отдаётся агенту сессии вместе с трасером ленты.
    """

    def __init__(self, mark: LogUserMark) -> None:
        self._mark = mark
        self._runs: dict[str, RunProgress] = {}

    def _say(self, message: str, *args: Any) -> None:
        with self._mark.applied():
            logger.info(message, *args)

    async def model_called(self, call: ModelCallStarted) -> None:
        now = time.monotonic()
        run = RunProgress(run_id=call.run, started=now)
        self._runs[run.run_id] = run

        self._say(
            "llm %s %s: run=%s model=%s messages=%d tools=%d",
            LlmStage.REQUEST.value,
            LlmStageEvent.STARTED.value,
            run.label,
            call.model,
            call.messages,
            call.tools,
        )

    async def model_token(self, token: ModelToken) -> None:
        run = self._runs.get(token.run)
        if run is None:
            return

        now = time.monotonic()
        if not run.first_token:
            run.first_token = now
            self._say("llm first token: run=%s in %dms", run.label, run.elapsed_ms(now))

        if token.reasoning:
            self._advance(run, LlmStage.THINKING, token.reasoning, now)
            return

        if not token.text:
            return

        self._advance(run, LlmStage.ANSWER, token.text, now)

    def _advance(
        self, run: RunProgress, stage: LlmStage, text: str, now: float
    ) -> None:
        current = run.stage
        if current is not None and current.stage is stage:
            current.add(text)
            return

        if current is not None:
            self._finish_stage(run, current, now)

        started = StageProgress(stage=stage, started=now)
        started.add(text)
        run.stage = started
        self._say(
            "llm %s %s: run=%s",
            stage.value,
            LlmStageEvent.STARTED.value,
            run.label,
        )

    def _finish_stage(self, run: RunProgress, stage: StageProgress, now: float) -> None:
        self._say(
            "llm %s %s: run=%s %d chars in %dms",
            stage.stage.value,
            LlmStageEvent.FINISHED.value,
            run.label,
            stage.chars,
            stage.elapsed_ms(now),
        )
        run.stage = None

    async def model_replied(self, finish: ModelCallFinished) -> None:
        run = self._runs.pop(finish.run, None)
        if run is None:
            return

        now = time.monotonic()
        if run.stage is not None:
            self._finish_stage(run, run.stage, now)

        reply = finish.reply
        if reply is None:
            reply = AssistantMessage(id=None, text="")

        if not run.first_token:
            self._complete_stages(run, reply)

        names: list[str] = []
        for call in reply.calls:
            names.append(call.tool)

        usage = reply.usage
        self._say(
            "llm %s %s: run=%s tokens in=%d out=%d (%d reasoning), "
            "tool_calls=[%s] in %dms",
            LlmStage.REQUEST.value,
            LlmStageEvent.FINISHED.value,
            run.label,
            usage.input_tokens,
            usage.output_tokens,
            usage.reasoning_tokens,
            ", ".join(names),
            run.elapsed_ms(now),
        )

    def _complete_stages(self, run: RunProgress, reply: AssistantMessage) -> None:
        """Журналирует стадии ответа без стрима: текст пришёл разом в итоговом
        сообщении.
        """
        if reply.reasoning:
            self._say(
                "llm %s %s: run=%s %d chars",
                LlmStage.THINKING.value,
                LlmStageEvent.COMPLETE.value,
                run.label,
                len(reply.reasoning),
            )

        if not reply.text:
            return

        self._say(
            "llm %s %s: run=%s %d chars",
            LlmStage.ANSWER.value,
            LlmStageEvent.COMPLETE.value,
            run.label,
            len(reply.text),
        )

    async def model_failed(self, run: str, error: BaseException) -> None:
        progress = self._runs.pop(run, None)
        if progress is None:
            return

        now = time.monotonic()
        if progress.stage is not None:
            self._finish_stage(progress, progress.stage, now)

        self._say(
            "llm %s %s: run=%s in %dms: %s",
            LlmStage.REQUEST.value,
            LlmStageEvent.FAILED.value,
            progress.label,
            progress.elapsed_ms(now),
            error,
        )
