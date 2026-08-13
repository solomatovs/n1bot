"""Журнал обмена с провайдером: запрос, рассуждения, ответ, вызовы инструментов.

Пишет строку на каждую смену состояния прогона, помечая её пользователем и
тредом: колбэки инструментов приходят из чужого event loop'а, где контекста
сессии chainlit уже нет.

Ошибки: своих не выпускает; сбой уходит колбэк-менеджеру langchain.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, ClassVar, Final
from uuid import UUID

from langchain_core.callbacks import AsyncCallbackHandler
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGenerationChunk, GenerationChunk, LLMResult
from pydantic import BaseModel, ConfigDict
from typing_extensions import override

from boba.chainlit.agent.chat_model import GeneratedMessage, ReasoningText
from boba.chainlit.domain.session import LogUserMark

__all__ = ["LlmStage", "LlmStageEvent", "LlmStateLog"]

logger = logging.getLogger(__name__)


class LlmStage(StrEnum):
    """Состояния, которые проходит один прогон модели."""

    REQUEST = "request"
    THINKING = "thinking"
    ANSWER = "answer"
    TOOL = "tool"


class LlmStageEvent(StrEnum):
    """Что случилось со стадией."""

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
        """Расход знает только ответ модели; у остальных сообщений его нет."""
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
    """Идущая стадия прогона: чем занят провайдер и сколько уже отдал."""

    stage: LlmStage
    started: float
    chars: int = 0

    def add(self, text: str) -> None:
        self.chars += len(text)

    def elapsed_ms(self, now: float) -> int:
        return int((now - self.started) * 1000)


@dataclass
class RunProgress:
    """Один прогон модели: от отправки запроса до финального сообщения."""

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
    """Вызов инструмента: имя, идентификатор вызова и его начало."""

    name: str
    call_id: str
    started: float

    def elapsed_ms(self, now: float) -> int:
        return int((now - self.started) * 1000)


class LlmStateLog(AsyncCallbackHandler):
    """Колбэк-обработчик: пишет в лог смену состояний обмена с провайдером."""

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
        """Ответ без стрима: стадий не было, текст пришёл разом в сообщении."""
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
        """Инструменты зовёт только ответ модели: у прочих сообщений вызовов нет."""
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
