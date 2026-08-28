"""Колбэки langchain одного прогона: AgentTracer публикует события процесса ответа в
шину через TurnFeed, LlmStateLog пишет смену состояний прогона в журнал.

Ошибки: своих не выпускает; сбой публикации показывается в чат и журнал одним
разбором FailureReport, сбой журналирования уходит колбэк-менеджеру langchain.
"""

from __future__ import annotations

import functools
import logging
import time
from abc import abstractmethod
from collections.abc import Callable, Coroutine, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, ClassVar, Final, Protocol, TypeVar
from uuid import UUID

from langchain_core.callbacks import AsyncCallbackHandler
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langchain_core.outputs import ChatGenerationChunk, GenerationChunk, LLMResult
from langchain_core.runnables.config import ensure_config
from langchain_core.tracers.base import AsyncBaseTracer
from pydantic import BaseModel, ConfigDict
from typing_extensions import ParamSpec, override

from boba.chainlit.agent.flow import PrefetchStage
from boba.chainlit.chat.feed import TurnFeed
from boba.chainlit.rendering.chat_view import StepText
from boba.chainlit.rendering.errors import show_error
from boba.identity.errors import FailureReport
from boba.identity.session import LogUserMark
from boba.llm.chat import GeneratedMessage, ReasoningText
from boba.toolkit.failure import FailureText
from chainlit.context import context_var

__all__ = [
    "AgentTracer",
    "LlmStage",
    "LlmStageEvent",
    "LlmStateLog",
    "TracedStage",
    "TurnArtifacts",
]

logger = logging.getLogger(__name__)


_P = ParamSpec("_P")
_R = TypeVar("_R")


def _visible_failure(
    fn: Callable[_P, Coroutine[Any, Any, _R]],
) -> Callable[_P, Coroutine[Any, Any, _R | None]]:
    """Делает сбой колбэка видимым: langchain гасит его исключения в logger.warning."""

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
    """Порт незавершённых артефактов хода, которые ведёт трасер: вызовы инструментов
    и потоковые рассуждения.
    """

    @abstractmethod
    def open_tool(self, run_key: str, call_id: str) -> None: ...

    @abstractmethod
    def close_tool(self, run_key: str) -> str | None: ...

    @abstractmethod
    def add_reasoning(self, run_key: str, text: str) -> None: ...

    @abstractmethod
    def take_reasoning(self, run_key: str) -> str: ...


class AgentTracer(AsyncBaseTracer):
    """Трасер одного агентского цикла: публикует рассуждения, вызовы инструментов и
    их итоги в шину; учёт прогонов ведётся до публикации.
    """

    def __init__(self, feed: TurnFeed, state: TurnArtifacts) -> None:
        super().__init__()
        self._context = context_var.get()
        self._feed = feed
        self._state = state

    @property
    def feed(self) -> TurnFeed:
        """Производитель сообщений хода, в который трасер публикует события."""
        return self._feed

    @staticmethod
    def _key_of(message_id: str | None, run_id: UUID) -> str:
        """Ключ шага рассуждений: id сообщения модели, а без него — id прогона."""
        if message_id:
            return message_id

        return str(run_id)

    def _set_context(self) -> None:
        context_var.set(self._context)

    @override
    @_visible_failure
    async def on_llm_new_token(
        self,
        token: str,
        *,
        chunk: GenerationChunk | ChatGenerationChunk | None = None,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **kwargs: Any,
    ) -> None:
        self._set_context()
        traced = await super().on_llm_new_token(
            token,
            chunk=chunk,
            run_id=run_id,
            parent_run_id=parent_run_id,
            **kwargs,
        )

        message = GeneratedMessage.of_chunk(chunk)
        if message is None:
            return traced

        reasoning = ReasoningText.of(message)
        if not reasoning:
            return traced

        self._state.add_reasoning(str(run_id), reasoning)
        await self._feed.thinking_token(self._key_of(message.id, run_id), reasoning)

        return traced

    @override
    @_visible_failure
    async def on_llm_end(
        self,
        response: LLMResult,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **kwargs: Any,
    ) -> None:
        self._set_context()
        traced = await super().on_llm_end(
            response,
            run_id=run_id,
            parent_run_id=parent_run_id,
            **kwargs,
        )

        streamed = self._state.take_reasoning(str(run_id))
        await self._feed.thinking_closed()

        # рассуждения без стрима приходят разом в итоговом сообщении
        if streamed:
            return traced

        message = GeneratedMessage.of_result(response)
        if message is None:
            return traced

        text = ReasoningText.of(message)
        if not text:
            return traced

        await self._feed.thinking_complete(self._key_of(message.id, run_id), text)

        return traced

    @override
    @_visible_failure
    async def on_llm_error(
        self,
        error: BaseException,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        tags: list[str] | None = None,
        **kwargs: Any,
    ) -> None:
        self._set_context()
        traced = await super().on_llm_error(
            error,
            run_id=run_id,
            parent_run_id=parent_run_id,
            tags=tags,
            **kwargs,
        )

        await self._feed.thinking_closed()

        return traced

    @override
    @_visible_failure
    async def on_tool_start(
        self,
        serialized: dict[str, Any],
        input_str: str,
        *,
        run_id: UUID,
        tags: list[str] | None = None,
        parent_run_id: UUID | None = None,
        metadata: dict[str, Any] | None = None,
        name: str | None = None,
        inputs: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        self._set_context()
        traced = await super().on_tool_start(
            serialized,
            input_str,
            run_id=run_id,
            parent_run_id=parent_run_id,
            tags=tags,
            metadata=metadata,
            name=name,
            inputs=inputs,
            **kwargs,
        )

        tool_name = name
        if not tool_name and serialized:
            tool_name = serialized.get("name")

        if not tool_name:
            tool_name = "tool"

        call_id = str(run_id)
        if given := kwargs.get(CallField.TOOL_CALL_ID.value):
            call_id = str(given)

        args: Mapping[str, Any] = {}
        if inputs:
            args = inputs

        await self._feed.tool_started(call_id, tool_name, args)
        self._state.open_tool(str(run_id), call_id)

        return traced

    @override
    @_visible_failure
    async def on_tool_end(
        self,
        output: Any,
        *,
        run_id: UUID,
        **kwargs: Any,
    ) -> None:
        self._set_context()
        traced = await super().on_tool_end(output, run_id=run_id, **kwargs)

        call_id = self._state.close_tool(str(run_id))
        if call_id is None:
            return traced

        # результат без конверта tool_call рисуется как есть: он и есть артефакт
        if not isinstance(output, ToolMessage):
            await self._feed.tool_finished(call_id, output)
            return traced

        if output.status == "error":
            await self._feed.tool_failed(call_id, str(output.content))
            return traced

        artifact = output.artifact
        if artifact is None:
            artifact = output

        await self._feed.tool_finished(call_id, artifact)

        return traced

    @override
    @_visible_failure
    async def on_tool_error(
        self,
        error: BaseException,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        tags: list[str] | None = None,
        **kwargs: Any,
    ) -> None:
        self._set_context()
        traced = await super().on_tool_error(
            error,
            run_id=run_id,
            parent_run_id=parent_run_id,
            tags=tags,
            **kwargs,
        )

        if call_id := self._state.close_tool(str(run_id)):
            await self._feed.tool_failed(call_id, FailureText.of(error))

        return traced

    @override
    async def _persist_run(self, run: Any) -> None:
        pass


class TracedStage(PrefetchStage):
    """Стадия подготовки ответа, которая отчитывается в ленту через производитель
    текущего прогона.
    """

    def __init__(self, name: str) -> None:
        self._name = name

    async def begin(self) -> None:
        feed = self._feed()
        if feed is None:
            return

        await feed.stage_started(self._name, StepText.REPHRASING.value)

    async def searching(self, queries: Sequence[str]) -> None:
        feed = self._feed()
        if feed is None:
            return

        await feed.stage_queries(self._name, queries)

    async def end(self, queries: Sequence[str], elapsed_ms: int) -> None:
        feed = self._feed()
        if feed is None:
            return

        await feed.stage_ended(self._name, queries, elapsed_ms)

    @staticmethod
    def _feed() -> TurnFeed | None:
        """Производитель сообщений текущего прогона; None, если ход идёт без ленты (cli,
        тесты).
        """
        config = ensure_config()

        callbacks = config.get("callbacks")
        handlers = getattr(callbacks, "handlers", None)
        if not handlers:
            return None

        for handler in handlers:
            if isinstance(handler, AgentTracer):
                return handler.feed

        return None


class LlmStage(StrEnum):
    """Состояния, которые проходит один прогон модели: запрос, рассуждение, ответ,
    инструмент.
    """

    REQUEST = "request"
    THINKING = "thinking"
    ANSWER = "answer"
    TOOL = "tool"


class LlmStageEvent(StrEnum):
    """Что случилось со стадией прогона: началась, закончилась, упала или пришла
    целиком.
    """

    STARTED = "started"
    FINISHED = "finished"
    FAILED = "failed"
    COMPLETE = "complete"
    """Стадия пришла разом, без стрима: у неё нет начала и конца во времени."""


class CallField(StrEnum):
    """Ключи вызова инструмента в колбэках langchain."""

    NAME = "name"
    TOOL_CALL_ID = "tool_call_id"
    INVOCATION_PARAMS = "invocation_params"


class ToolCallField:
    """Ключи langchain-ToolCall: у TypedDict pyright принимает только литерал."""

    NAME: Final = "name"


class UsageField:
    """Ключи langchain-UsageMetadata: тот же контракт TypedDict."""

    INPUT: Final = "input_tokens"
    OUTPUT: Final = "output_tokens"


class InvocationParams(BaseModel):
    """Параметры вызова, которые langchain кладёт в колбэк старта прогона."""

    model_config = ConfigDict(extra="ignore")

    model: str = ""
    tools: Sequence[Mapping[str, Any]] = ()

    @classmethod
    def of(cls, kwargs: Mapping[str, Any]) -> InvocationParams:
        raw = kwargs.get(CallField.INVOCATION_PARAMS.value)
        if not isinstance(raw, Mapping):
            return cls()

        return cls.model_validate(raw)


class LlmUsage(BaseModel):
    """Расход токенов прогона по данным провайдера."""

    model_config = ConfigDict(extra="ignore")

    input_tokens: int = 0
    output_tokens: int = 0

    @classmethod
    def of(cls, message: BaseMessage | None) -> LlmUsage:
        """Расход токенов из ответа модели; у остальных сообщений его нет."""
        if not isinstance(message, AIMessage):
            return cls()

        usage = message.usage_metadata
        if usage is None:
            return cls()

        return cls(
            input_tokens=usage[UsageField.INPUT],
            output_tokens=usage[UsageField.OUTPUT],
        )


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


@dataclass
class ToolProgress:
    """Идущий вызов инструмента: имя, идентификатор вызова и момент старта."""

    name: str
    call_id: str
    started: float

    def elapsed_ms(self, now: float) -> int:
        return int((now - self.started) * 1000)


class LlmStateLog(AsyncCallbackHandler):
    """Колбэк-обработчик, который пишет в журнал каждую смену состояния обмена с
    провайдером и длительность стадий.
    """

    def __init__(self, mark: LogUserMark) -> None:
        super().__init__()
        self._mark = mark
        self._runs: dict[str, RunProgress] = {}
        self._tools: dict[str, ToolProgress] = {}

    def _say(self, message: str, *args: Any) -> None:
        with self._mark.applied():
            logger.info(message, *args)

    @staticmethod
    def _key(run_id: UUID) -> str:
        return str(run_id)

    @override
    async def on_chat_model_start(
        self,
        serialized: dict[str, Any],
        messages: list[list[Any]],
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        now = time.monotonic()
        run = RunProgress(run_id=self._key(run_id), started=now)
        self._runs[run.run_id] = run

        sent = 0
        for batch in messages:
            sent += len(batch)

        params = InvocationParams.of(kwargs)
        self._say(
            "llm %s %s: run=%s model=%s messages=%d tools=%d",
            LlmStage.REQUEST.value,
            LlmStageEvent.STARTED.value,
            run.label,
            params.model,
            sent,
            len(params.tools),
        )

    @override
    async def on_llm_new_token(
        self,
        token: str | list[str | dict[str, Any]],
        *,
        chunk: GenerationChunk | ChatGenerationChunk | None = None,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        tags: list[str] | None = None,
        **kwargs: Any,
    ) -> None:
        run = self._runs.get(self._key(run_id))
        if run is None:
            return

        now = time.monotonic()
        if not run.first_token:
            run.first_token = now
            self._say("llm first token: run=%s in %dms", run.label, run.elapsed_ms(now))

        message = GeneratedMessage.of_chunk(chunk)
        if reasoning := ReasoningText.of(message):
            self._advance(run, LlmStage.THINKING, reasoning, now)
            return

        text = self._text_of(token)
        if not text:
            return

        self._advance(run, LlmStage.ANSWER, text, now)

    @staticmethod
    def _text_of(token: str | list[str | dict[str, Any]]) -> str:
        if isinstance(token, str):
            return token

        parts: list[str] = []
        for part in token:
            if isinstance(part, str):
                parts.append(part)

        return "".join(parts)

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

    @override
    async def on_llm_end(
        self,
        response: LLMResult,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        tags: list[str] | None = None,
        **kwargs: Any,
    ) -> None:
        run = self._runs.pop(self._key(run_id), None)
        if run is None:
            return

        now = time.monotonic()
        if run.stage is not None:
            self._finish_stage(run, run.stage, now)

        message = GeneratedMessage.of_result(response)
        if not run.first_token:
            self._complete_stages(run, message)

        usage = LlmUsage.of(message)
        self._say(
            "llm %s %s: run=%s tokens in=%d out=%d, tool_calls=[%s] in %dms",
            LlmStage.REQUEST.value,
            LlmStageEvent.FINISHED.value,
            run.label,
            usage.input_tokens,
            usage.output_tokens,
            ", ".join(self._tool_names_of(message)),
            run.elapsed_ms(now),
        )

    def _complete_stages(self, run: RunProgress, message: BaseMessage | None) -> None:
        """Журналирует стадии ответа без стрима: текст пришёл разом в итоговом
        сообщении.
        """
        if reasoning := ReasoningText.of(message):
            self._say(
                "llm %s %s: run=%s %d chars",
                LlmStage.THINKING.value,
                LlmStageEvent.COMPLETE.value,
                run.label,
                len(reasoning),
            )

        text = self._content_of(message)
        if not text:
            return

        self._say(
            "llm %s %s: run=%s %d chars",
            LlmStage.ANSWER.value,
            LlmStageEvent.COMPLETE.value,
            run.label,
            len(text),
        )

    @staticmethod
    def _content_of(message: BaseMessage | None) -> str:
        if message is None:
            return ""

        content = message.content
        if isinstance(content, str):
            return content

        return str(content)

    @staticmethod
    def _tool_names_of(message: BaseMessage | None) -> list[str]:
        """Имена инструментов, которые зовёт ответ модели; у прочих сообщений вызовов
        нет.
        """
        if not isinstance(message, AIMessage):
            return []

        names: list[str] = []
        for call in message.tool_calls:
            names.append(call[ToolCallField.NAME])

        return names

    @override
    async def on_llm_error(
        self,
        error: BaseException,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        tags: list[str] | None = None,
        **kwargs: Any,
    ) -> None:
        run = self._runs.pop(self._key(run_id), None)
        if run is None:
            return

        now = time.monotonic()
        if run.stage is not None:
            self._finish_stage(run, run.stage, now)

        self._say(
            "llm %s %s: run=%s in %dms: %s",
            LlmStage.REQUEST.value,
            LlmStageEvent.FAILED.value,
            run.label,
            run.elapsed_ms(now),
            error,
        )

    @override
    async def on_tool_start(
        self,
        serialized: dict[str, Any],
        input_str: str,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        inputs: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        call = ToolProgress(
            name=self._tool_name_of(serialized, kwargs),
            call_id=self._call_id_of(kwargs),
            started=time.monotonic(),
        )
        self._tools[self._key(run_id)] = call

        self._say(
            "%s %s %s: call=%s args=%d chars",
            LlmStage.TOOL.value,
            call.name,
            LlmStageEvent.STARTED.value,
            call.call_id,
            len(input_str),
        )

    @staticmethod
    def _tool_name_of(serialized: Mapping[str, Any], kwargs: Mapping[str, Any]) -> str:
        name = kwargs.get(CallField.NAME.value)
        if not name and serialized:
            name = serialized.get(CallField.NAME.value)
        if not name:
            return LlmStage.TOOL.value

        return str(name)

    @staticmethod
    def _call_id_of(kwargs: Mapping[str, Any]) -> str:
        call_id = kwargs.get(CallField.TOOL_CALL_ID.value)
        if not call_id:
            return "-"

        return str(call_id)

    @override
    async def on_tool_end(
        self,
        output: Any,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        tags: list[str] | None = None,
        **kwargs: Any,
    ) -> None:
        call = self._tools.pop(self._key(run_id), None)
        if call is None:
            return

        self._say(
            "%s %s %s: call=%s output=%d chars in %dms",
            LlmStage.TOOL.value,
            call.name,
            LlmStageEvent.FINISHED.value,
            call.call_id,
            len(self._content_of(output)),
            call.elapsed_ms(time.monotonic()),
        )

    @override
    async def on_tool_error(
        self,
        error: BaseException,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        tags: list[str] | None = None,
        **kwargs: Any,
    ) -> None:
        call = self._tools.pop(self._key(run_id), None)
        if call is None:
            return

        self._say(
            "%s %s %s: call=%s in %dms: %s",
            LlmStage.TOOL.value,
            call.name,
            LlmStageEvent.FAILED.value,
            call.call_id,
            call.elapsed_ms(time.monotonic()),
            error,
        )
