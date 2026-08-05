"""Лента чата: единственное место, знающее раскладку и отрисовку результатов."""

from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from collections.abc import Mapping
from typing import Any, ClassVar, cast
from uuid import UUID, uuid5

from literalai.observability.step import TrueStepType

from boba.cancellation import StopReason
from boba.chainlit.rendering.chart_figure import build_plotly_element
from boba.chainlit.rendering.result_view import (
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

__all__ = ["ChatSink", "ChatView", "LiveSink", "RecordingSink"]

logger = logging.getLogger(__name__)


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
    """Строит step-иерархию хода диалога и пишет её в sink."""

    CONTAINER_NAME: ClassVar[str] = "process..."
    RUNNING_TEXT: ClassVar[str] = "выполняется"
    STOPPED_TEXT: ClassVar[str] = "остановлено пользователем"
    DISCONNECTED_TEXT: ClassVar[str] = "остановлено при разрыве связи"
    USER_MESSAGE: ClassVar[TrueStepType] = cast("TrueStepType", "user_message")
    ASSISTANT_MESSAGE: ClassVar[TrueStepType] = cast(
        "TrueStepType", "assistant_message"
    )
    NAMESPACE: ClassVar[UUID] = UUID("6f9b1f4e-2f1a-4c1a-9a2f-1d3b5c7e9a11")
    # Dingbats \u0438 Geometric Shapes: \u044d\u043c\u043e\u0434\u0437\u0438-\u043a\u0440\u0443\u0436\u043a\u0438 \u0442\u0440\u0435\u0431\u0443\u044e\u0442 \u0448\u0440\u0438\u0444\u0442\u0430 Unicode 12,
    # \u043d\u0430 \u0441\u0442\u0430\u0440\u044b\u0445 \u043a\u043b\u0438\u0435\u043d\u0442\u0430\u0445 \u0432\u043c\u0435\u0441\u0442\u043e \u043d\u0438\u0445 \u0440\u0438\u0441\u0443\u0435\u0442\u0441\u044f \u043f\u0443\u0441\u0442\u043e\u0439 \u043a\u0432\u0430\u0434\u0440\u0430\u0442
    IDLE: ClassVar[str] = "\u25cb"
    DONE: ClassVar[str] = "\u2714"
    FAILED: ClassVar[str] = "\u2716"

    @classmethod
    def titled(cls, status: str, name: str) -> str:
        """Название шага со статусным кружком слева."""
        return f"{status} {name}"

    @classmethod
    def stopped_text(cls, reason: StopReason | None) -> str:
        """Формулировка остановки: пользователь нажал stop или ушёл из сессии."""
        if reason is StopReason.DISCONNECT:
            return cls.DISCONNECTED_TEXT
        return cls.STOPPED_TEXT

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
        self._container: Step | None = None
        self._tool_names: dict[str, str] = {}

    def end_turn(self) -> None:
        self._container = None

    async def question(self, text: str, step_id: str | None = None) -> Step:
        step = self._step(
            self._user_name,
            self.USER_MESSAGE,
            parent_id=None,
            step_id=step_id,
        )
        step.output = text
        await self._sink.put(step)
        return step

    async def answer(self, text: str, key: str | None = None) -> Step:
        step = self._step(
            self._assistant_name,
            self.ASSISTANT_MESSAGE,
            parent_id=None,
            step_id=self.derive_id(self._thread_id, key, "answer"),
        )
        step.output = text
        await self._sink.put(step)
        return step

    async def error(self, text: str, key: str | None = None) -> Step:
        step = self._step(
            self._assistant_name,
            self.ASSISTANT_MESSAGE,
            parent_id=None,
            step_id=self.derive_id(self._thread_id, key, "error"),
        )
        step.output = text
        step.is_error = True
        await self._sink.put(step)
        return step

    def open_answer(self, key: str | None = None) -> Message:
        message = Message(content="", id=self.derive_id(self._thread_id, key, "answer"))
        message.parent_id = None
        return message

    async def container(self, key: str | None = None) -> Step:
        if self._container is not None:
            return self._container
        step = self._step(
            self.CONTAINER_NAME,
            "run",
            parent_id=None,
            step_id=self.derive_id(self._thread_id, key, "process"),
        )
        await self._sink.put(step)
        self._container = step
        return step

    async def thinking(self, text: str, key: str | None = None) -> Step:
        step = await self._child(
            self.titled(self.IDLE, "thinking"), "llm", key, "thinking"
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
        step = await self._child(self.titled(self.IDLE, name), "tool", key, "tool")
        self._tool_names[step.id] = name
        if args:
            step.input = self._render_args(args)
        step.output = self.RUNNING_TEXT
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
            step.name = self.titled(self.DONE, self._tool_names.get(step.id, step.name))
            await self._sink.put(step)
            return

        failed = not result.ok
        step.name = self.titled(
            self.FAILED if failed else self.DONE,
            self._tool_names.get(step.id, step.name),
        )
        match ToolResultView(result).render():
            case ChartRendering(spec=spec, title=title):
                step.output = (
                    f"график отрисован: {title}" if title else "график отрисован"
                )
                await self._sink.put(step)
                await self._chart(title, spec, tool_call_id)
            case MarkdownRendering(markdown=markdown):
                step.output = markdown
                step.is_error = failed
                await self._sink.put(step)

    async def tool_stopped(self, step: Step, note: str) -> None:
        """Инструмент не доработал: ход остановлен."""
        step.name = self.titled(self.FAILED, self._tool_names.get(step.id, step.name))
        step.output = note
        step.end = utc_now()
        await self._sink.put(step)

    async def tool_failed(self, step: Step, error: object) -> None:
        step.is_error = True
        step.name = self.titled(self.FAILED, self._tool_names.get(step.id, step.name))
        step.output = f"**tool failed:** {error}"
        step.end = utc_now()
        await self._sink.put(step)

    async def _chart(
        self,
        title: str | None,
        spec: Mapping[str, Any],
        tool_call_id: str | None,
    ) -> None:
        step = self._step(
            self._assistant_name,
            self.ASSISTANT_MESSAGE,
            parent_id=None,
            step_id=self.derive_id(self._thread_id, tool_call_id, "chart"),
        )
        step.output = title or ""
        if self._sink.EMITS_ELEMENTS:
            element = build_plotly_element(title or "chart", dict(spec))
            element.id = str(
                self.derive_id(self._thread_id, tool_call_id, "element") or element.id
            )
            step.elements = [element]
        await self._sink.put(step)

    @classmethod
    def derive_id(cls, thread_id: str, key: str | None, role: str) -> str | None:
        if not key:
            return None
        return str(uuid5(cls.NAMESPACE, f"{thread_id}/{key}/{role}"))

    async def _child(
        self,
        name: str,
        step_type: TrueStepType,
        key: str | None,
        role: str,
    ) -> Step:
        container = await self.container(key)
        return self._step(
            name,
            step_type,
            parent_id=container.id,
            step_id=self.derive_id(self._thread_id, key, role),
        )

    def _step(
        self,
        name: str,
        step_type: TrueStepType,
        *,
        parent_id: str | None,
        step_id: str | None = None,
    ) -> Step:
        return Step(
            name=name,
            type=step_type,
            id=step_id,
            parent_id=parent_id,
            thread_id=self._thread_id,
            default_open=False,
            auto_collapse=True,
        )

    @staticmethod
    def _render_args(args: Mapping[str, Any]) -> str:
        return json.dumps(dict(args), ensure_ascii=False, indent=2, default=str)
