"""Лента чата: единственное место, знающее раскладку и отрисовку результатов."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, ClassVar, cast
from uuid import UUID, uuid5

from literalai.observability.step import TrueStepType

from boba.cancellation import StopReason
from boba.canvas.canvas import CanvasAction
from boba.chainlit.canvas.panel import ToolStreams
from boba.chainlit.rendering.tool import (
    ChartRendering,
    CustomElementRendering,
    DiagramRendering,
    MarkdownRendering,
    ToolCallMarkdown,
    ToolResultView,
)
from boba.toolkit.calls import ToolCallViews, ToolIntent
from boba.toolkit.failure import FailureText
from boba.toolkit.result import ToolArtifact
from chainlit.config import config as chainlit_config
from chainlit.context import context
from chainlit.element import CustomElement
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
    "ToolStepLabel",
    "TurnDraft",
    "TurnPulse",
]

logger = logging.getLogger(__name__)


class StepText(StrEnum):
    """Тексты шагов ленты."""

    CONTAINER = "process..."
    REQUESTED = "process... (request sent)"
    """Подпись контейнера, пока модель молчит: ход уже идёт, токенов ещё нет."""

    RUNNING = "running"
    THINKING = "thinking"
    TOOL = "tool"
    PREFETCH = "context lookup"
    """Этап подготовки контекста: под ним лежат вызовы предварительного поиска."""
    REPHRASING = "rephrasing the question"
    """Первая фаза подготовки: поисковых запросов ещё нет."""
    STOPPED = "stopped by the user"
    ABORTED = "stopped"
    FINISHED = "finished"
    TURN_FAILED = "turn failed"

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

    def timed(self, name: str, elapsed_ms: int) -> str:
        """То же название с длительностью вызова; неизмеренное время опускается."""
        label = StepElapsed.of(elapsed_ms)
        if not label:
            return self.title(name)

        return f"{self.title(name)} · {label}"


class StepElapsed:
    """Длительность вызова в названии шага."""

    SECOND_MS: ClassVar[int] = 1000
    MINUTE_MS: ClassVar[int] = 60 * SECOND_MS

    @classmethod
    def of(cls, elapsed_ms: int) -> str:
        """Подпись длительности; 0 и отрицательное — время не измерено."""
        if elapsed_ms <= 0:
            return ""

        if elapsed_ms < cls.SECOND_MS:
            return f"{elapsed_ms} ms"

        if elapsed_ms < cls.MINUTE_MS:
            return f"{elapsed_ms / cls.SECOND_MS:.1f} s"

        minutes = elapsed_ms // cls.MINUTE_MS
        seconds = (elapsed_ms % cls.MINUTE_MS) / cls.SECOND_MS
        return f"{minutes} m {seconds:.0f} s"


class ToolStepLabel:
    """Название шага инструмента: имя тула и подпись вызова, если она пришла."""

    SEPARATOR: ClassVar[str] = " · "

    @classmethod
    def of(cls, name: str, intent: str) -> str:
        if not intent:
            return name

        return f"{name}{cls.SEPARATOR}{intent}"


class StepRole(StrEnum):
    """Роль шага в детерминированном id: один ключ — несколько шагов."""

    ANSWER = "answer"
    ERROR = "error"
    PROCESS = "process"
    PULSE = "pulse"
    STAGE = "stage"
    THINKING = "thinking"
    TOOL = "tool"
    CHART = "chart"
    ELEMENT = "element"
    STREAM = "stream"


@dataclass
class TurnDraft:
    """Открытый ход: что он уже нарисовал и что ещё стримится.

    Ответ и рассуждения дописываются токенами, поэтому живут здесь до закрытия;
    ключ хода адресует контейнер и все его ответы.
    """

    key: str | None = None
    container: Step | None = None
    answer: Message | None = None
    answers: int = 0
    thinking: Step | None = None
    stage: Step | None = None
    """Открытый этап: пока он жив, шаги хода вкладываются в него, а не в контейнер."""

    def seal_stage(self) -> Step | None:
        """Снимает открытый этап; None — этап не открывали."""
        step = self.stage
        if step is None:
            return None

        self.stage = None
        return step

    def answer_key(self, key: str | None = None) -> str | None:
        """Каждый следующий ответ хода — своё сообщение, как в истории."""
        turn_key = key
        if turn_key is None:
            turn_key = self.key

        if turn_key is None:
            return None

        if not self.answers:
            return turn_key

        return f"{turn_key}#{self.answers}"

    def next_answer_key(self) -> str | None:
        """Ключ очередного ответа хода: следующий вызов даст уже новый."""
        key = self.answer_key()
        self.answers += 1
        return key

    def seal_answer(self) -> Message | None:
        """Снимает открытый ответ; None — открывать было нечего."""
        message = self.answer
        if message is None:
            return None

        self.answer = None
        self.answers += 1
        return message

    def seal_thinking(self) -> Step | None:
        """Снимает открытый шаг рассуждений; None — модель ничего не надумала."""
        step = self.thinking
        if step is None:
            return None

        self.thinking = None
        return step


class ChatSink(ABC):
    """Куда уходят нарисованные шаги."""

    EMITS_ELEMENTS: ClassVar[bool] = False

    @abstractmethod
    async def put(self, step: Step) -> None:
        pass

    @abstractmethod
    async def drop(self, step: Step) -> None:
        """Убирает шаг из ленты; следующий put ставит его в конец заново."""


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

    async def drop(self, step: Step) -> None:
        if step.id not in self._sent:
            return

        self._sent.discard(step.id)
        # снятый шаг уходит заново отправкой, а chainlit шлёт её лишь однажды
        step.persisted = False
        await context.emitter.delete_step(step.to_dict())


class RecordingSink(ChatSink):
    """Копит шаги как StepDict — для отдачи истории треда."""

    def __init__(self) -> None:
        self._steps: dict[str, StepDict] = {}

    async def put(self, step: Step) -> None:
        self._steps[step.id] = step.to_dict()

    async def drop(self, step: Step) -> None:
        self._steps.pop(step.id, None)

    @property
    def steps(self) -> list[StepDict]:
        return list(self._steps.values())


class TurnPulse:
    """Кружок ожидания в конце ленты: ход идёт, чем бы он сейчас ни был занят.

    Стримящийся ответ рисует курсор сам, поэтому на время стрима пульс гасится:
    кружок в ленте всегда ровно один. Любой новый шаг ленты встаёт ниже пульса,
    поэтому показ снимает кружок и рисует заново — так он остаётся последним.
    """

    CONTENT: ClassVar[str] = "\u200b"
    """Zero-width space: chainlit рисует его мигающим кружком."""

    def __init__(self, sink: ChatSink, step: Step) -> None:
        self._sink = sink
        self._step = step
        self._live = False
        self._shown = False

    @property
    def step(self) -> Step | None:
        """Показанный шаг пульса; None — кружка в ленте сейчас нет."""
        if not self._shown:
            return None

        return self._step

    async def start(self) -> None:
        """Ход начался: кружок держится до его конца."""
        self._live = True
        await self._draw()

    async def hold(self) -> None:
        """Пошёл стрим ответа: курсор рисует само сообщение."""
        await self._erase()

    async def resume(self) -> None:
        """Ответ закрыт: кружок возвращается в конец ленты."""
        if not self._live:
            return

        await self._draw()

    async def bump(self) -> None:
        """Ниже пульса встал новый шаг: кружок переезжает в конец ленты."""
        if not self._shown:
            return

        await self._draw()

    async def stop(self) -> None:
        """Ход завершён: кружка в ленте быть не должно."""
        self._live = False
        await self._erase()

    async def _draw(self) -> None:
        await self._erase()
        await self._sink.put(self._step)
        self._shown = True

    async def _erase(self) -> None:
        if not self._shown:
            return

        await self._sink.drop(self._step)
        self._shown = False


class ChatView:
    """Строит step-иерархию хода диалога и пишет её в sink.

    Контракт id: каждый шаг адресуется детерминированно через derive_id, чтобы
    live-отрисовка и повтор из истории давали одинаковую ленту. Ключи:
    контейнер и ответ — id вопроса (turn key), thinking — id AIMessage,
    tool/chart/element — tool_call_id.

    Стримящиеся элементы хода — ответ и рассуждения — живут в TurnDraft: пока
    ход открыт, их дописывают токен за токеном, и лишь закрытие уходит в sink.

    Пока ход не закончен, в конце ленты держится кружок ожидания (TurnPulse) —
    независимо от того, думает модель, зовёт инструмент или молчит.
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
        display_name = user_name
        if not display_name:
            display_name = "User"
        self._user_name = display_name
        self._assistant_name = chainlit_config.ui.name
        self._turn = TurnDraft()
        self._tool_names: dict[str, str] = {}
        self._pulse = self._new_pulse(None)

    @property
    def thread_id(self) -> str:
        """Тред ленты: к нему адресуются потоки инструментов."""
        return self._thread_id

    @property
    def container_step(self) -> Step | None:
        """Открытый контейнер процесса; None — ход шагов ещё не рисовал."""
        return self._turn.container

    @property
    def answer_message(self) -> Message | None:
        """Стримящийся ответ хода; None — ни одного токена ещё не было."""
        return self._turn.answer

    @property
    def thinking_step(self) -> Step | None:
        """Открытый шаг рассуждений; None — модель ещё ничего не надумала."""
        return self._turn.thinking

    @property
    def pulse_step(self) -> Step | None:
        """Показанный кружок ожидания; None — ход ничего не ждёт."""
        return self._pulse.step

    @property
    def stage_step(self) -> Step | None:
        """Открытый этап хода; None — шаги идут прямо в контейнер."""
        return self._turn.stage

    def begin_turn(self, key: str | None) -> None:
        """Открывает ход: его ключ адресует контейнер и ответ."""
        self._turn = TurnDraft(key=key)
        self._pulse = self._new_pulse(key)

    async def finish_turn(self) -> None:
        """Ход закончен любым исходом: кружок ожидания снимается."""
        await self._pulse.stop()

    def _new_pulse(self, key: str | None) -> TurnPulse:
        step = self._step(
            self._assistant_name,
            StepKind.ASSISTANT,
            parent_id=None,
            step_id=self.derive_id(self._thread_id, key, StepRole.PULSE),
        )
        step.output = TurnPulse.CONTENT
        return TurnPulse(self._sink, step)

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
        await self._pulse.bump()
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
        await self._pulse.bump()
        return step

    async def stream_answer(self, token: str, key: str | None = None) -> None:
        """Токен в стримящийся ответ; первое обращение открывает сообщение."""
        if self._turn.answer is None:
            self._turn.answer = self._open_answer(key)

        await self._pulse.hold()
        await self._turn.answer.stream_token(token)

    async def close_answer(self, key: str | None = None) -> None:
        """Финальная отправка ответа; пустой ход тоже получает сообщение."""
        if self._turn.answer is None:
            self._turn.answer = self._open_answer(key)

        await self._turn.answer.send()

    async def rewrite_answer(self, content: str, key: str | None = None) -> None:
        """Замещает текст ответа целиком: фиксация прерванного стрима."""
        if self._turn.answer is None:
            await self.answer(content, key)
            return

        self._turn.answer.content = content
        await self._turn.answer.send()

    async def _seal_answer(self) -> None:
        """Закрывает текущий ответ перед инструментом.

        Иначе текст после инструмента дописывался бы в сообщение, открытое до
        него, и результат тула вставал бы в ленте ниже всего ответа — а при
        сборке истории он оказывается между текстами. Здесь live приводится к
        тому же порядку.
        """
        message = self._turn.seal_answer()
        if message is None:
            return

        await message.send()
        await self._pulse.resume()

    def _open_answer(self, key: str | None = None) -> Message:
        answer_key = self._turn.answer_key(key)
        message = Message(
            content="",
            id=self.derive_id(self._thread_id, answer_key, StepRole.ANSWER),
        )
        message.parent_id = None
        return message

    async def container(self, name: str = StepText.CONTAINER) -> Step:
        if self._turn.container is not None:
            return self._turn.container

        step = self._step(
            name,
            StepKind.RUN,
            parent_id=None,
            step_id=self.derive_id(self._thread_id, self._turn.key, StepRole.PROCESS),
        )
        await self._sink.put(step)
        await self._pulse.bump()
        self._turn.container = step
        return step

    async def await_model(self) -> Step:
        """Контейнер до первого токена: ход виден сразу, ещё до ответа модели.

        Иначе лента молчит всё время инференса, и пользователь не понимает,
        принят ли его вопрос вообще.
        """
        step = await self.container(StepText.REQUESTED)
        await self._pulse.start()

        if step.name == StepText.REQUESTED:
            return step

        step.name = StepText.REQUESTED
        await self._sink.put(step)
        return step

    async def model_answered(self) -> None:
        """Первый токен пришёл: контейнер снова обычный «process...»."""
        step = self._turn.container
        if step is None:
            return

        if step.name != StepText.REQUESTED:
            return

        step.name = StepText.CONTAINER
        await self._sink.put(step)

    async def thinking(self, text: str, key: str | None = None) -> Step:
        """Готовые рассуждения одним шагом: сборка ленты из истории."""
        step = await self._child(
            StepStatus.IDLE.title(StepText.THINKING),
            StepKind.LLM,
            key,
            StepRole.THINKING,
        )
        step.output = text
        stamp = utc_now()
        step.start = stamp
        step.end = stamp
        await self._sink.put(step)
        return step

    async def stream_thinking(self, token: str, key: str | None = None) -> None:
        """Токен рассуждения; первое обращение открывает шаг под контейнером."""
        if self._turn.thinking is None:
            self._turn.thinking = await self._open_thinking(key)

        await self._turn.thinking.stream_token(token)

    async def close_thinking(self) -> None:
        """Закрывает шаг рассуждений; без открытого шага закрывать нечего."""
        step = self._turn.seal_thinking()
        if step is None:
            return

        step.end = utc_now()
        await self._sink.put(step)

    async def _open_thinking(self, key: str | None) -> Step:
        """Шаг не уходит в sink сразу: его откроет первый же stream_token."""
        step = await self._child(
            StepStatus.IDLE.title(StepText.THINKING),
            StepKind.LLM,
            key,
            StepRole.THINKING,
        )
        step.start = utc_now()
        return step

    async def begin_stage(self, name: str, phase: str) -> Step:
        """Открывает этап хода: шаги до его закрытия вкладываются внутрь."""
        await self._seal_answer()

        step = await self._child(
            StepStatus.IDLE.title(name), StepKind.RUN, self._turn.key, StepRole.STAGE
        )
        step.output = phase
        step.start = utc_now()
        await self._sink.put(step)

        self._turn.stage = step
        return step

    async def stage_queries(self, queries: Sequence[str]) -> None:
        """Подписывает открытый этап запросами: фаза подготовки сменилась."""
        step = self._turn.stage
        if step is None:
            return

        step.output = self.stage_output(queries)
        await self._sink.put(step)

    async def end_stage(self, queries: Sequence[str], elapsed_ms: int) -> None:
        """Закрывает этап и подписывает его запросами; без этапа закрывать нечего."""
        step = self._turn.seal_stage()
        if step is None:
            return

        step.name = StepStatus.DONE.timed(step.name.split(" ", 1)[-1], elapsed_ms)
        step.output = self.stage_output(queries)
        ended = utc_now()
        step.start = ended
        step.end = ended
        await self._sink.put(step)

    @staticmethod
    def stage_output(queries: Sequence[str]) -> str:
        """Подпись этапа: запросы, с которыми он работал, по строке на запрос."""
        lines: list[str] = []
        for query in queries:
            lines.append(f"- {query}")

        if not lines:
            return StepText.FINISHED.value

        return "\n".join(lines)

    async def tool_started(
        self,
        name: str,
        args: Mapping[str, Any] | None,
        key: str | None = None,
    ) -> Step:
        await self._seal_answer()

        call_args: Mapping[str, Any] = {}
        intent = ""
        if args:
            call_args = ToolIntent.without(args)
            intent = ToolIntent.of(args)

        label = ToolStepLabel.of(name, intent)

        step = await self._child(
            StepStatus.IDLE.title(label), StepKind.TOOL, key, StepRole.TOOL
        )
        self._tool_names[step.id] = label

        if call_args:
            rendering = ToolCallMarkdown(ToolCallViews.of(name), call_args).render()
            if rendering is not None:
                step.input = rendering.markdown
                step.show_input = rendering.show_input

        # фронт chainlit рисует секцию output (с inline-элементами, в том числе
        # кнопкой живого вывода) только при непустом output шага
        step.output = StepText.RUNNING
        step.start = utc_now()

        if button := self._stream_button(name, key):
            step.elements = [button]

        await self._sink.put(step)
        return step

    def _stream_button(self, name: str, key: str | None) -> CustomElement | None:
        """Кнопка живого вывода: только live-лента и только потоковые тулы."""
        if not self._sink.EMITS_ELEMENTS:
            return None

        if not key:
            return None

        if not ToolStreams.streamable(name):
            return None

        element = CustomElement(
            name="CanvasStream",
            props={str(CanvasAction.CALL_ID): key, "label": name},
            thread_id=self._thread_id,
        )
        element_id = self.derive_id(self._thread_id, key, StepRole.STREAM)
        if element_id:
            element.id = element_id

        return element

    async def tool_finished(
        self,
        step: Step,
        artifact: Any,
        tool_call_id: str | None = None,
    ) -> None:
        # завершённость шага = start == end; иначе фронт считает его живым
        ended = utc_now()
        step.start = ended
        step.end = ended
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
        step.name = status.timed(
            self._tool_names.get(step.id, step.name), result.elapsed_ms
        )
        match ToolResultView(result).render():
            case ChartRendering() as chart:
                step.output = "chart rendered"
                if chart.title:
                    step.output = f"chart rendered: {chart.title}"
                await self._sink.put(step)
                await self._element(chart, tool_call_id)
            case CustomElementRendering() as custom:
                step.output = "element rendered"
                if custom.title:
                    step.output = f"element rendered: {custom.title}"
                await self._sink.put(step)
                await self._element(custom, tool_call_id)
            case DiagramRendering() as diagram:
                step.output = "diagram rendered"
                if diagram.title:
                    step.output = f"diagram rendered: {diagram.title}"
                await self._sink.put(step)
                await self._element(diagram, tool_call_id)
            case MarkdownRendering(markdown=markdown):
                step.output = markdown
                step.is_error = failed
                await self._sink.put(step)

    async def tool_stopped(self, step: Step, note: str) -> None:
        """Инструмент не доработал: ход остановлен."""
        step.name = StepStatus.FAILED.title(self._tool_names.get(step.id, step.name))
        step.output = note
        ended = utc_now()
        step.start = ended
        step.end = ended
        await self._sink.put(step)

    async def tool_failed(self, step: Step, error: object) -> None:
        """Провал инструмента; текст сбоя совпадает с тем, что получает модель."""
        text = str(error)
        if isinstance(error, BaseException):
            text = FailureText.of(error)

        step.is_error = True
        step.name = StepStatus.FAILED.title(self._tool_names.get(step.id, step.name))
        step.output = f"**tool failed:** {text}"
        ended = utc_now()
        step.start = ended
        step.end = ended
        await self._sink.put(step)

    async def _element(
        self,
        rendering: ChartRendering | CustomElementRendering | DiagramRendering,
        tool_call_id: str | None,
    ) -> None:
        """Шаг-носитель элемента: график и кастомный компонент рисуются одинаково."""
        step = self._step(
            self._assistant_name,
            StepKind.ASSISTANT,
            parent_id=None,
            step_id=self.derive_id(self._thread_id, tool_call_id, StepRole.CHART),
        )

        title = rendering.title
        if not title:
            title = ""
        step.output = title

        if self._sink.EMITS_ELEMENTS:
            element = rendering.chat_element()
            element_id = self.derive_id(self._thread_id, tool_call_id, StepRole.ELEMENT)
            if not element_id:
                element_id = element.id
            element.id = str(element_id)
            step.elements = [element]

        await self._sink.put(step)
        await self._pulse.bump()

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
        parent = await self._parent()
        return self._step(
            name,
            kind,
            parent_id=parent.id,
            step_id=self.derive_id(self._thread_id, key, role),
        )

    async def _parent(self) -> Step:
        """Родитель очередного шага: открытый этап, иначе контейнер хода."""
        if self._turn.stage is not None:
            return self._turn.stage

        return await self.container()

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
