"""Лента чата: единственное место, знающее раскладку и отрисовку результатов."""

from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from collections.abc import Mapping
from enum import StrEnum
from typing import Any, ClassVar, cast
from uuid import UUID, uuid5

from literalai.observability.step import TrueStepType

from boba.cancellation import StopReason
from boba.chainlit.rendering.result import (
    ChartRendering,
    MarkdownRendering,
    ToolResultView,
)
from boba.toolkit.result import ToolArtifact
from chainlit.config import config as chainlit_config
from chainlit.langchain.callbacks import process_content
from chainlit.message import Message
from chainlit.step import Step, StepDict
from chainlit.utils import utc_now

__all__ = [
    "ChatSink",
    "ChatView",
    "LiveSink",
    "RecordingSink",
    "StepKind",
    "StepRole",
    "StepStatus",
    "StepText",
]

logger = logging.getLogger(__name__)


class StepText(StrEnum):
    """Тексты шагов ленты."""

    CONTAINER = "process..."
    RUNNING = "выполняется"
    STOPPED = "остановлено пользователем"
    ABORTED = "остановлено"

    @classmethod
    def for_stop(cls, reason: StopReason | None) -> StepText:
        """Формулировка остановки: кнопка пользователя или снятая снаружи задача."""
        if reason is StopReason.USER_STOP:
            return cls.STOPPED
        return cls.ABORTED


class StepKind(StrEnum):
    """Типы шагов chainlit, которыми пользуется лента."""

    USER = "user_message"
    ASSISTANT = "assistant_message"
    RUN = "run"
    TOOL = "tool"
    LLM = "llm"

    @property
    def step_type(self) -> TrueStepType:
        return cast("TrueStepType", self.value)


class StepStatus(StrEnum):
    """Статусный кружок в названии шага."""

    IDLE = "○"
    DONE = "✔"
    FAILED = "✖"

    def title(self, name: str) -> str:
        """Название шага со статусным кружком слева."""
        return f"{self.value} {name}"


class StepRole(StrEnum):
    """Роль шага в детерминированном id: один ключ — несколько шагов."""

    ANSWER = "answer"
    ERROR = "error"
    PROCESS = "process"
    THINKING = "thinking"
    TOOL = "tool"
    CHART = "chart"
    ELEMENT = "element"


class ChatSink(ABC):
    """Куда уходят нарисованные шаги."""

    EMITS_ELEMENTS: ClassVar[bool] = False

    @abstractmethod
    async def put(self, step: Step) -> None:
        pass


class LiveSink(ChatSink):
    """Отдаёт шаги в открытую сессию chainlit."""

    EMITS_ELEMENTS: ClassVar[bool] = True

    def __init__(self) -> None:
        self._sent: set[str] = set()

    async def put(self, step: Step) -> None:
        if step.id in self._sent:
            await step.update()
            return
        self._sent.add(step.id)
        await step.send()


class RecordingSink(ChatSink):
    """Копит шаги как StepDict — для отдачи истории треда."""

    def __init__(self) -> None:
        self._steps: dict[str, StepDict] = {}

    async def put(self, step: Step) -> None:
        self._steps[step.id] = step.to_dict()

    @property
    def steps(self) -> list[StepDict]:
        return list(self._steps.values())


class ChatView:
    """Строит step-иерархию хода диалога и пишет её в sink.

    Контракт id: каждый шаг адресуется детерминированно через derive_id, чтобы
    live-отрисовка и повтор из истории давали одинаковую ленту. Ключи:
    контейнер и ответ — id вопроса (turn key), thinking — id AIMessage,
    tool/chart/element — tool_call_id.
    """

    NAMESPACE: ClassVar[UUID] = UUID("6f9b1f4e-2f1a-4c1a-9a2f-1d3b5c7e9a11")

    def __init__(
        self,
        thread_id: str,
        sink: ChatSink,
        *,
        user_name: str | None = None,
    ) -> None:
        self._thread_id = thread_id
        self._sink = sink
        self._user_name = user_name or "User"
        self._assistant_name = chainlit_config.ui.name
        self._turn_key: str | None = None
        self._container: Step | None = None
        self._answer: Message | None = None
        self._tool_names: dict[str, str] = {}

    @property
    def container_step(self) -> Step | None:
        """Открытый контейнер процесса; None — ход шагов ещё не рисовал."""
        return self._container

    @property
    def answer_message(self) -> Message | None:
        """Стримящийся ответ хода; None — ни одного токена ещё не было."""
        return self._answer

    def begin_turn(self, key: str | None) -> None:
        """Открывает ход: его ключ адресует контейнер и ответ."""
        self._turn_key = key
        self._container = None
        self._answer = None

    async def question(self, text: str, step_id: str | None = None) -> Step:
        step = self._step(
            self._user_name,
            StepKind.USER,
            parent_id=None,
            step_id=step_id,
        )
        step.output = text
        await self._sink.put(step)
        return step

    async def answer(self, text: str, key: str | None = None) -> Step:
        step = self._step(
            self._assistant_name,
            StepKind.ASSISTANT,
            parent_id=None,
            step_id=self.derive_id(self._thread_id, key, StepRole.ANSWER),
        )
        step.output = text
        await self._sink.put(step)
        return step

    async def error(self, text: str, key: str | None = None) -> Step:
        step = self._step(
            self._assistant_name,
            StepKind.ASSISTANT,
            parent_id=None,
            step_id=self.derive_id(self._thread_id, key, StepRole.ERROR),
        )
        step.output = text
        step.is_error = True
        await self._sink.put(step)
        return step

    async def stream_answer(self, token: str, key: str | None = None) -> None:
        """Токен в стримящийся ответ; первое обращение открывает сообщение."""
        if self._answer is None:
            self._answer = self._open_answer(key)
        await self._answer.stream_token(token)

    async def close_answer(self, key: str | None = None) -> None:
        """Финальная отправка ответа; пустой ход тоже получает сообщение."""
        if self._answer is None:
            self._answer = self._open_answer(key)
        await self._answer.send()

    async def rewrite_answer(self, content: str, key: str | None = None) -> None:
        """Замещает текст ответа целиком: фиксация прерванного стрима."""
        if self._answer is None:
            await self.answer(content, key)
            return
        self._answer.content = content
        await self._answer.send()

    def _open_answer(self, key: str | None = None) -> Message:
        message = Message(
            content="",
            id=self.derive_id(self._thread_id, key, StepRole.ANSWER),
        )
        message.parent_id = None
        return message

    async def container(self) -> Step:
        if self._container is not None:
            return self._container
        step = self._step(
            StepText.CONTAINER,
            StepKind.RUN,
            parent_id=None,
            step_id=self.derive_id(self._thread_id, self._turn_key, StepRole.PROCESS),
        )
        await self._sink.put(step)
        self._container = step
        return step

    async def thinking(self, text: str, key: str | None = None) -> Step:
        step = await self._child(
            StepStatus.IDLE.title("thinking"), StepKind.LLM, key, StepRole.THINKING
        )
        step.output = text
        step.start = utc_now()
        step.end = utc_now()
        await self._sink.put(step)
        return step

    async def tool_started(
        self,
        name: str,
        args: Mapping[str, Any] | None,
        key: str | None = None,
    ) -> Step:
        step = await self._child(
            StepStatus.IDLE.title(name), StepKind.TOOL, key, StepRole.TOOL
        )
        self._tool_names[step.id] = name
        if args:
            step.input = self._render_args(args)
        step.output = StepText.RUNNING
        step.start = utc_now()
        await self._sink.put(step)
        return step

    async def tool_finished(
        self,
        step: Step,
        artifact: Any,
        tool_call_id: str | None = None,
    ) -> None:
        step.end = utc_now()
        result = ToolArtifact.revive(artifact)
        if result is None:
            content, lang = process_content(artifact)
            step.output = content
            step.language = lang
            step.name = StepStatus.DONE.title(self._tool_names.get(step.id, step.name))
            await self._sink.put(step)
            return

        failed = not result.ok
        status = StepStatus.DONE
        if failed:
            status = StepStatus.FAILED
        step.name = status.title(self._tool_names.get(step.id, step.name))
        match ToolResultView(result).render():
            case ChartRendering() as chart:
                step.output = "график отрисован"
                if chart.title:
                    step.output = f"график отрисован: {chart.title}"
                await self._sink.put(step)
                await self._chart(chart, tool_call_id)
            case MarkdownRendering(markdown=markdown):
                step.output = markdown
                step.is_error = failed
                await self._sink.put(step)

    async def tool_stopped(self, step: Step, note: str) -> None:
        """Инструмент не доработал: ход остановлен."""
        step.name = StepStatus.FAILED.title(self._tool_names.get(step.id, step.name))
        step.output = note
        step.end = utc_now()
        await self._sink.put(step)

    async def tool_failed(self, step: Step, error: object) -> None:
        step.is_error = True
        step.name = StepStatus.FAILED.title(self._tool_names.get(step.id, step.name))
        step.output = f"**tool failed:** {error}"
        step.end = utc_now()
        await self._sink.put(step)

    async def _chart(
        self,
        chart: ChartRendering,
        tool_call_id: str | None,
    ) -> None:
        step = self._step(
            self._assistant_name,
            StepKind.ASSISTANT,
            parent_id=None,
            step_id=self.derive_id(self._thread_id, tool_call_id, StepRole.CHART),
        )
        step.output = chart.title or ""
        if self._sink.EMITS_ELEMENTS:
            element = chart.plotly_element()
            element.id = str(
                self.derive_id(self._thread_id, tool_call_id, StepRole.ELEMENT)
                or element.id
            )
            step.elements = [element]
        await self._sink.put(step)

    @classmethod
    def derive_id(cls, thread_id: str, key: str | None, role: StepRole) -> str | None:
        if not key:
            return None
        return str(uuid5(cls.NAMESPACE, f"{thread_id}/{key}/{role}"))

    async def _child(
        self,
        name: str,
        kind: StepKind,
        key: str | None,
        role: StepRole,
    ) -> Step:
        container = await self.container()
        return self._step(
            name,
            kind,
            parent_id=container.id,
            step_id=self.derive_id(self._thread_id, key, role),
        )

    def _step(
        self,
        name: str,
        kind: StepKind,
        *,
        parent_id: str | None,
        step_id: str | None = None,
    ) -> Step:
        return Step(
            name=name,
            type=kind.step_type,
            id=step_id,
            parent_id=parent_id,
            thread_id=self._thread_id,
            default_open=False,
            auto_collapse=True,
        )

    @staticmethod
    def _render_args(args: Mapping[str, Any]) -> str:
        return json.dumps(dict(args), ensure_ascii=False, indent=2, default=str)
