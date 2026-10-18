"""Лента чата: единственное место, знающее раскладку и отрисовку результатов."""

from __future__ import annotations

import asyncio
import base64
import binascii
import csv
import io
import json
import logging
import re
from abc import ABC, abstractmethod
from collections import deque
from collections.abc import (
    AsyncIterator,
    Awaitable,
    Callable,
    Iterator,
    Mapping,
    Sequence,
)
from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Any, ClassVar, Protocol, TypeAlias, cast
from urllib.parse import unquote, urlsplit, urlunsplit
from uuid import UUID, uuid5

import mcp_types as mt
from acp.experimental.v2 import schema
from chainlit.config import config as chainlit_config
from chainlit.context import context
from chainlit.element import CustomElement, Element, File, Image
from chainlit.message import Message
from chainlit.step import Step
from chainlit.utils import utc_now
from literalai.observability.step import TrueStepType

from boba.aid.chat.ui_text import UiText
from boba.aid.domain.keys import CanvasFileUrl
from boba.aid.rendering.question import QuestionCard
from boba.aid.rendering.tool import AppProp, ChatElements, ToolAppView
from boba.canvas.canvas import (
    CanvasAction,
    CanvasElement,
    CanvasError,
    CanvasKind,
    CanvasRegistry,
    CanvasScope,
)
from boba.canvas.storage import StorageError, StorageNotFoundError
from boba.runtime.storage import StorageClient
from boba.toolkit.calls import CallViews, ToolCallBase, ToolCallModels, ToolIntent
from boba.toolkit.mcp import McpToolNames
from boba.toolkit.result import (
    BlockAudience,
    ContentMime,
    Fence,
    JsonBlock,
    NoteLine,
    TableText,
    VisualResult,
)
from boba.transport.http.connection import UrlScheme

__all__ = [
    "AppCancel",
    "AppCard",
    "ChatSink",
    "ChatView",
    "LiveSink",
    "StepKind",
    "StepMeta",
    "StepRole",
    "StepStatus",
    "StepText",
    "StreamBatch",
    "ToolStepLabel",
    "TurnDraft",
    "TurnPulse",
    "WorkspaceFile",
    "WorkspaceLinks",
]

logger = logging.getLogger(__name__)


class StepText(StrEnum):
    """Ключи подписей ленты в переводах chainlit: строку на языке интерфейса
    даёт ChatView.label."""

    CONTAINER = "aid.feed.working"
    """Контейнер идущего хода."""

    DONE = "aid.feed.done"
    """Контейнер законченного хода без шагов."""

    DONE_STEPS = "aid.feed.doneSteps"
    """Контейнер законченного хода с числом его шагов."""

    RUNNING = "aid.feed.running"
    THINKING = "aid.feed.thinking"
    TOOL = "aid.feed.tool"
    STOPPED = "aid.feed.stoppedByUser"
    FINISHED = "aid.feed.unfinished"
    """Выход вызова, который не закончился к концу хода."""

    NO_ROWS = "aid.feed.noRows"
    """Пустая таблица результата."""


class StepKind(StrEnum):
    """Типы шагов chainlit, которыми пользуется лента."""

    USER = "user_message"
    ASSISTANT = "assistant_message"
    RUN = "run"
    TOOL = "tool"
    LLM = "llm"

    @property
    def step_type(self) -> TrueStepType:
        # chainlit объявляет тип шага без сообщений (user/assistant_message), хотя
        # сам создаёт такие шаги: тип библиотеки уже, чем её значения
        return cast("TrueStepType", self.value)


class StepLanguage(StrEnum):
    """Язык подсветки выхода шага, каким его знает фронт chainlit."""

    TEXT = "text"
    JSON = "json"


class StepStatus(StrEnum):
    """Статусный знак в названии шага: идёт, готово, провал и остановка.

    Провал (ошибка инструмента) рисуется ещё и цветом ошибки (is_error шага);
    остановленный вызов — ход прервали раньше, чем он закончился, — не
    ошибка и отмечается нейтрально.
    """

    IDLE = "○"
    DONE = "✔"
    FAILED = "✖"
    STOPPED = "⊘"

    def title(self, name: str) -> str:
        """Название шага со статусным кружком слева."""
        return f"{self.value} {name}"

    def timed(self, name: str, elapsed: str) -> str:
        """То же название с длительностью вызова; пустая длительность опускается."""
        if not elapsed:
            return self.title(name)

        return f"{self.title(name)}{ToolStepLabel.SEPARATOR}{elapsed}"


class ElapsedText(StrEnum):
    """Ключи единиц длительности вызова в переводах chainlit."""

    MILLISECONDS = "aid.feed.elapsedMs"
    SECONDS = "aid.feed.elapsedSec"
    MINUTES = "aid.feed.elapsedMin"


class StepElapsed:
    """Длительность вызова в названии шага, единицами языка интерфейса: и
    подпись, и её разбор — регулярное выражение по тем же переводам.

    Создаётся лентой (ChatView) из её подписей; разбором пользуются тесты,
    сверяющие название шага с длительностью любой величины.
    """

    SECOND_MS: ClassVar[int] = 1000
    MINUTE_MS: ClassVar[int] = 60 * SECOND_MS
    INTEGER: ClassVar[str] = r"\d+"
    DECIMAL: ClassVar[str] = r"\d+\.\d"
    MARK: ClassVar[str] = "\x00"

    def __init__(self, texts: UiText) -> None:
        self._texts = texts

    def of(self, elapsed_ms: int) -> str:
        """Подпись длительности; 0 и отрицательное — время не измерено."""
        if elapsed_ms <= 0:
            return ""

        if elapsed_ms < self.SECOND_MS:
            return self._texts.text(ElapsedText.MILLISECONDS.value, ms=elapsed_ms)

        if elapsed_ms < self.MINUTE_MS:
            seconds = f"{elapsed_ms / self.SECOND_MS:.1f}"
            return self._texts.text(ElapsedText.SECONDS.value, sec=seconds)

        minutes = elapsed_ms // self.MINUTE_MS
        rest = f"{(elapsed_ms % self.MINUTE_MS) / self.SECOND_MS:.0f}"
        return self._texts.text(ElapsedText.MINUTES.value, min=minutes, sec=rest)

    def titled(self, title: str, timed: bool) -> str:
        """Регулярное выражение названия шага title с длительностью: timed —
        длительность обязательна, иначе может не быть."""
        shapes = "|".join(self._shapes())
        tail = f"(?:{re.escape(ToolStepLabel.SEPARATOR)}(?:{shapes}))"
        if not timed:
            tail = f"{tail}?"

        return f"^{re.escape(title)}{tail}$"

    def _shapes(self) -> Iterator[str]:
        yield self._shape(ElapsedText.MILLISECONDS, ms=self.INTEGER)
        yield self._shape(ElapsedText.SECONDS, sec=self.DECIMAL)
        yield self._shape(ElapsedText.MINUTES, min=self.INTEGER, sec=self.INTEGER)

    def _shape(self, key: ElapsedText, **numbers: str) -> str:
        """Подпись с метками вместо чисел, экранированная, с числами-шаблонами."""
        marks: dict[str, str] = {}
        for name in numbers:
            marks[name] = f"{self.MARK}{name}{self.MARK}"

        shape = re.escape(self._texts.text(key.value, **marks))
        for name, number in numbers.items():
            shape = shape.replace(re.escape(marks[name]), number)

        return shape


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
    THINKING = "thinking"
    TOOL = "tool"
    CHART = "chart"
    ELEMENT = "element"
    STREAM = "stream"
    APP = "app"
    """Элемент ToolApp карточки приложения MCP Apps."""

    APP_MESSAGE = "app-message"
    """Сообщение ленты с карточкой приложения MCP Apps."""

    QUESTION = "question"
    QUESTION_CARD = "question-card"


class StepMeta(StrEnum):
    """Ключи metadata шага, которые читает overlay ленты chainlit
    (Message/index.tsx)."""

    AVATAR_NAME = "avatarName"
    """Аватар по этому имени вместо имени шага: у контейнера имя меняется
    со счётчиком шагов."""

    APP = "mcpApp"
    """Сообщение карточки приложения MCP Apps: без аватара и кнопок
    сообщения, рамка встаёт с края текста ответа."""


class AppCancel(StrEnum):
    """Причина в ui/notifications/tool-cancelled приложению, чей вызов
    кончился без итога tools/call."""

    STOPPED = "the turn ended before the call did"
    NO_RESULT = "the call ended without a tools/call result"


@dataclass(frozen=True)
class AppCard:
    """Карточка приложения MCP Apps идущего вызова: сообщение основной
    ленты под контейнером хода и элемент ToolApp в нём. Исход вызова
    ChatView дописывает в props элемента и отправляет сообщение заново."""

    message: Step
    element: CustomElement


@dataclass
class TurnDraft:
    """Открытый ход: что он уже нарисовал и что ещё стримится.

    Ответ и рассуждения дописываются токенами, поэтому живут здесь до закрытия;
    ключ хода адресует контейнер и все его ответы.
    """

    key: str | None = None
    container: Step | None = None
    steps: int = 0
    """Шаги хода под контейнером: их число — в подписи законченного хода."""

    answer: Message | None = None
    answers: int = 0
    thinking: Step | None = None

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

    @abstractmethod
    def rebuilt(self) -> None:
        """Лента собрана заново из истории: всё отправленное раньше забыто,
        следующий put любого шага — отправка, а не обновление."""


class LiveSink(ChatSink):
    """Отдаёт шаги в открытую сессию chainlit.

    Помнит id отправленных шагов: повторный put того же id — обновление.
    После пересборки ленты из истории (правка вопроса) память сбрасывается,
    иначе шаги нового хода с прежними id ушли бы обновлениями к шагам,
    которых во вкладке больше нет.
    """

    EMITS_ELEMENTS: ClassVar[bool] = True

    def __init__(self) -> None:
        self._sent: set[str] = set()

    async def put(self, step: Step) -> None:
        if step.id in self._sent:
            await step.update()
            return

        self._sent.add(step.id)
        await step.send()

    def rebuilt(self) -> None:
        self._sent.clear()

    async def drop(self, step: Step) -> None:
        if step.id not in self._sent:
            return

        self._sent.discard(step.id)
        # снятый шаг уходит заново отправкой, а chainlit шлёт её лишь однажды
        step.persisted = False
        await context.emitter.delete_step(step.to_dict())


class Streamable(Protocol):
    """Шаг ленты, принимающий токены: chainlit Step и Message подходят структурно."""

    @abstractmethod
    async def stream_token(self, token: str) -> None: ...


class StreamBatch:
    """Склейка токенов стримящегося шага: кадр уходит в ленту не чаще окна.

    Кадр на каждый токен заставлял фронт перерисовывать ленту на каждый токен.
    Токены копятся, таймер event loop отдаёт пачку одним stream_token; смена
    шага и любой другой кадр ленты (BatchedSink, закрытие ответа) сначала
    сбрасывают пачку, поэтому порядок кадров совпадает с порядком событий.
    Ошибка отложенной отправки не глотается: её поднимает ближайший flush.
    """

    WINDOW_SEC: ClassVar[float] = 0.05

    def __init__(self) -> None:
        self._parts: list[str] = []
        self._target: Streamable | None = None
        self._timer: asyncio.TimerHandle | None = None
        self._deferred: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()

    async def push(self, target: Streamable, token: str) -> None:
        """Токен в пачку; токен другого шага сначала выталкивает накопленное."""
        if self._target is not None and self._target is not target:
            await self.flush()

        self._target = target
        self._parts.append(token)

        if self._timer is None:
            loop = asyncio.get_running_loop()
            self._timer = loop.call_later(self.WINDOW_SEC, self._fire)

    async def flush(self) -> None:
        """Отдаёт накопленное сейчас; поднимает ошибку отложенной отправки."""
        self._cancel_timer()

        async with self._lock:
            await self._send()

        deferred = self._deferred
        if deferred is None:
            return

        if not deferred.done():
            return

        self._deferred = None
        deferred.result()

    def _fire(self) -> None:
        self._timer = None
        self._deferred = asyncio.create_task(self._send_locked())

    async def _send_locked(self) -> None:
        async with self._lock:
            await self._send()

    async def _send(self) -> None:
        target = self._target
        text = "".join(self._parts)
        self._parts.clear()

        if target is None:
            return

        if not text:
            return

        await target.stream_token(text)

    def _cancel_timer(self) -> None:
        timer = self._timer
        if timer is None:
            return

        timer.cancel()
        self._timer = None


class BatchedSink(ChatSink):
    """Sink, выталкивающий пачку токенов перед каждым своим кадром: шаг не может
    обогнать токены, отправленные раньше него.
    """

    def __init__(self, inner: ChatSink, batch: StreamBatch) -> None:
        self._inner = inner
        self._batch = batch

    @property
    def emits_elements(self) -> bool:
        return self._inner.EMITS_ELEMENTS

    async def put(self, step: Step) -> None:
        await self._batch.flush()
        await self._inner.put(step)

    async def drop(self, step: Step) -> None:
        await self._batch.flush()
        await self._inner.drop(step)

    def rebuilt(self) -> None:
        self._inner.rebuilt()


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


class ProgressText:
    """Текст блока прогресса идущего вызова по отчётам инструмента.

    Создаётся лентой (ChatView) на шаг вызова с его первым отчётом и живёт
    до конца вызова. Блок показывает последние LINES отчётов: новые приходят
    снизу, старые уходят сверху, поэтому сообщения текут, а не копятся. Доля
    сделанного уже стоит в тексте отчёта, его собирает агент.
    """

    LINES: ClassVar[int] = 10
    BREAK: ClassVar[str] = "\n"
    """Перенос строки: фронт рисует текст шага с сохранением переносов,
    поэтому каждое сообщение встаёт своей строкой без промежутка."""

    def __init__(self) -> None:
        self._lines: deque[str] = deque(maxlen=self.LINES)

    def shown(self, text: str) -> str:
        """Блок после очередного отчёта."""
        self._lines.append(text)

        return self.BREAK.join(self._lines)


BlockDrawing: TypeAlias = Callable[
    [mt.ContentBlock, Step, str | None, int], Awaitable[str]
]
"""Отрисовщик блока ответа: блок, шаг вызова, ключ вызова и номер блока-не-
текста; итог — markdown для выхода шага (пусто — блок стал элементом)."""

ResourceDrawing: TypeAlias = Callable[[str, str, Step, str | None, int], Awaitable[str]]
"""Отрисовщик текстового встроенного ресурса по его MIME: текст ресурса, его
имя (последний сегмент uri), шаг вызова, ключ вызова и номер блока-не-текста;
итог — markdown для выхода шага (пусто — ресурс стал элементом)."""


@dataclass(frozen=True)
class WorkspaceFile:
    """Файл workspace сервера, на который ведёт ссылка resource_link со
    схемой file://: путь в workspace и MIME — названный ссылкой, а без него
    по имени файла. Строит его WorkspaceLinks."""

    path: str
    mime: str


class WorkspaceLinks:
    """Разбор ссылок resource_link на файлы workspace сервера: одно правило
    для ленты (ChatView) и для показа панели по ответу tools/call
    (CanvasOpener), которые держат его в поле."""

    FILE_SCHEME: ClassVar[str] = "file"
    """Схема ссылки на файл workspace сервера."""

    def file_of(self, block: mt.ResourceLink) -> WorkspaceFile | None:
        """Файл ссылки; None — ссылка другой схемы, не в workspace."""
        address = urlsplit(str(block.uri))
        if address.scheme != self.FILE_SCHEME:
            return None

        path = unquote(address.path)

        return WorkspaceFile(path=path, mime=ContentMime.named(block.mime_type, path))


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

    Итог вызова — ответ сервера MCP: блоки для человека рисуются реестром по
    виду блока и MIME ресурса (таблица, блок кода, график, вьювер канваса
    или вложение); картинка и звук ложатся файлом в хранилище треда
    (storage) и рисуются как файл workspace; неизвестное — JSON без байтов.
    """

    NAMESPACE: ClassVar[UUID] = UUID("6f9b1f4e-2f1a-4c1a-9a2f-1d3b5c7e9a11")
    TEXT_FAMILY: ClassVar[str] = "text/"
    """Начало MIME текстовых ресурсов: неизвестный текст рисуется блоком кода."""
    TREE_BYTES: ClassVar[int] = 1 << 20
    """Предел JSON для дерева: больший документ рисуется блоком кода."""
    BASE64_SLICE: ClassVar[int] = 64 * 1024
    """Срез base64 картинки и звука при записи в хранилище: кратен 4,
    поэтому каждый срез декодируется сам по себе."""
    BYTES: ClassVar[Mapping[str, Any]] = {"data": True, "resource": {"blob": True}}
    """Поля байтов base64, которых JSON неизвестного блока не раскрывает."""

    def __init__(  # noqa: PLR0913 — лента треда собирается всеми своими входами
        self,
        thread_id: str,
        sink: ChatSink,
        user_key: str,
        storage: StorageClient,
        texts: UiText,
        *,
        user_name: str | None = None,
    ) -> None:
        self._thread_id = thread_id
        self._texts = texts
        self._elapsed = StepElapsed(texts)
        self._user_key = user_key
        self._links = WorkspaceLinks()
        self._storage = storage
        self._batch = StreamBatch()
        self._sink = BatchedSink(sink, self._batch)
        display_name = user_name
        if not display_name:
            display_name = "User"
        self._user_name = display_name
        self._assistant_name = chainlit_config.ui.name
        self._turn = TurnDraft()
        self._tool_names: dict[str, str] = {}
        self._awaiting: dict[str, AppCard] = {}
        """Карточка приложения MCP Apps идущего вызова по id его шага: ждёт
        исхода вызова."""
        self._progress: dict[str, ProgressText] = {}
        self._call_views = CallViews()
        self._names = McpToolNames()
        self._elements = ChatElements()
        self._drawings: dict[type[mt.ContentBlock], BlockDrawing] = {
            mt.TextContent: self._text,
            mt.EmbeddedResource: self._embedded,
            mt.ResourceLink: self._linked,
            mt.ImageContent: self._media,
            mt.AudioContent: self._media,
        }
        """Отрисовка блока ответа по его виду (type)."""
        self._resources: dict[str, ResourceDrawing] = {
            ContentMime.CSV: self._table,
            ContentMime.PLOTLY: self._chart,
            ContentMime.SQL: self._fenced,
            ContentMime.JSON: self._tree,
        }
        """Отрисовка встроенного ресурса по MIME; прочий text/* — блоком кода."""
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

    def label(self, key: StepText, **values: object) -> str:
        """Подпись ленты на языке интерфейса."""
        return self._texts.text(key.value, **values)

    def begin_turn(self, key: str | None) -> None:
        """Открывает ход: его ключ адресует контейнер и ответ."""
        self._turn = TurnDraft(key=key)
        self._pulse = self._new_pulse(key)

    async def finish_turn(self) -> None:
        """Ход закончен любым исходом: контейнер получает итоговую подпись
        с числом шагов, кружок ожидания снимается."""
        await self._batch.flush()
        await self._close_container()
        await self._pulse.stop()

    async def _close_container(self) -> None:
        step = self._turn.container
        if step is None:
            return

        name = self.label(StepText.DONE)
        if self._turn.steps:
            name = self.label(StepText.DONE_STEPS, count=self._turn.steps)

        step.name = name
        await self._sink.put(step)

    def rebuilt(self) -> None:
        """Вкладки собрали ленту заново из истории: шаги нового хода с прежними
        id (правка вопроса) уходят отправкой, а не обновлением."""
        self._sink.rebuilt()

    def _new_pulse(self, key: str | None) -> TurnPulse:
        step = self._step(
            self._assistant_name,
            StepKind.ASSISTANT,
            parent_id=None,
            step_id=self.derive_id(self._thread_id, key, StepRole.PULSE),
        )
        step.output = TurnPulse.CONTENT
        return TurnPulse(self._sink, step)

    async def question(
        self,
        text: str,
        step_id: str | None = None,
        name: str | None = None,
        links: Sequence[schema.ResourceContentBlock] = (),
    ) -> Step:
        """Шаг вопроса; name — автор, если он известен точнее имени ленты;
        вложения — файлы шага (_attachment)."""
        author = name
        if not author:
            author = self._user_name

        step = self._step(
            author,
            StepKind.USER,
            parent_id=None,
            step_id=step_id,
        )
        step.output = text
        if self._sink.emits_elements:
            attached = self._attached(links, step_id)
            step.elements = [element async for element in attached]

        await self._sink.put(step)
        return step

    async def _attached(
        self, links: Sequence[schema.ResourceContentBlock], step_id: str | None
    ) -> AsyncIterator[Element]:
        for number, link in enumerate(links):
            yield await self._attachment(link, step_id, number)

    async def _attachment(
        self, link: schema.ResourceContentBlock, step_id: str | None, number: int
    ) -> Element:
        """Файл вопроса по его ссылке: картинка — миниатюрой, файл, который
        показывает панель, — кнопкой открытия в панели, как файлы итогов
        вызовов, прочее — вложением для скачивания. Адрес — путь и запрос
        ссылки агента без хоста, который браузеру может быть не виден."""
        address = urlsplit(str(link.uri))
        url = urlunsplit(("", "", address.path, address.query, ""))
        mime = ContentMime.named(link.mime_type, link.name)
        viewer = CanvasRegistry.viewer_for(mime)
        if viewer is None:
            return self._file(link.name, url, mime, step_id, number)

        if viewer.kind is CanvasKind.IMAGE:
            image = Image(name=link.name, url=url, mime=mime, display="inline")
            self._numbered(image, step_id, number)
            return image

        try:
            scope = await self._scope()
        except StorageError as exc:
            logger.info(
                "thread %s: attachment %s of question %s stays a download, the "
                "workspace of the thread is unknown: %s",
                self._thread_id,
                link.uri,
                step_id,
                exc,
            )
            return self._file(link.name, url, mime, step_id, number)

        try:
            file_key = CanvasFileUrl.key(scope, address.path)
        except ValueError as exc:
            logger.info(
                "thread %s: attachment %s of question %s is not a file of the "
                "thread, it stays a download: %s",
                self._thread_id,
                link.uri,
                step_id,
                exc,
            )
            return self._file(link.name, url, mime, step_id, number)

        try:
            card = await viewer.link(file_key, mime)
        except CanvasError as exc:
            logger.info(
                "thread %s: the panel refused attachment %s of question %s, it "
                "stays a download: %s",
                self._thread_id,
                file_key.in_workspace(),
                step_id,
                exc,
            )
            return self._file(link.name, url, mime, step_id, number)

        element = self._elements.of_result(card)
        self._numbered(element, step_id, number)
        return element

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
        await self._batch.push(self._turn.answer, token)

    async def close_answer(self, key: str | None = None) -> None:
        """Финальная отправка ответа; пустой ход тоже получает сообщение."""
        if self._turn.answer is None:
            self._turn.answer = self._open_answer(key)

        await self._batch.flush()
        await self._turn.answer.send()

    async def rewrite_answer(self, content: str, key: str | None = None) -> None:
        """Замещает текст ответа целиком: фиксация прерванного стрима."""
        if self._turn.answer is None:
            await self.answer(content, key)
            return

        await self._batch.flush()
        self._turn.answer.content = content
        await self._turn.answer.send()

    async def interrupt_answer(self, note: str, key: str | None = None) -> None:
        """Дописывает к накопленному ответу курсивную пометку об остановке; без
        ответа пометка становится очередным ответом хода. Накопленное — вместе
        с токенами, ещё не ушедшими из пачки.
        """
        marker = f"_{note}_"
        if self._turn.answer is None:
            self._turn.answer = self._open_answer(key)

        await self._batch.flush()
        partial = self._turn.answer.content
        if not partial:
            await self.rewrite_answer(marker, key)
            return

        await self.rewrite_answer(f"{partial}\n\n{marker}", key)

    async def seal_answer(self) -> None:
        """Закрывает текущий ответ перед инструментом или новым сообщением модели.

        Иначе текст после инструмента дописывался бы в сообщение, открытое до
        него, и результат тула вставал бы в ленте ниже всего ответа — а при
        сборке истории он оказывается между текстами. Здесь live приводится к
        тому же порядку.
        """
        message = self._turn.seal_answer()
        if message is None:
            return

        await self._batch.flush()
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

    async def container(self) -> Step:
        """Контейнер хода: появляется с первым шагом под ним — вживую и при
        воспроизведении истории одинаково; ход из одного ответа контейнера
        не имеет."""
        if self._turn.container is not None:
            return self._turn.container

        step = self._step(
            self.label(StepText.CONTAINER),
            StepKind.RUN,
            parent_id=None,
            step_id=self.derive_id(self._thread_id, self._turn.key, StepRole.PROCESS),
        )
        # корневой шаг грузит аватар по имени, а имя контейнера меняется со счётчиком
        step.metadata = {StepMeta.AVATAR_NAME.value: self._assistant_name}
        await self._sink.put(step)
        await self._pulse.bump()
        self._turn.container = step
        return step

    async def await_model(self) -> None:
        """Ход ждёт модель: в конце ленты встаёт кружок ожидания, так что
        принятый вопрос виден сразу, ещё до первого токена."""
        await self._pulse.start()

    async def stream_thinking(self, token: str, key: str | None = None) -> None:
        """Токен рассуждения; первое обращение открывает шаг под контейнером."""
        if self._turn.thinking is None:
            self._turn.thinking = await self._open_thinking(key)

        await self._batch.push(self._turn.thinking, token)

    async def close_thinking(self) -> None:
        """Закрывает шаг рассуждений: кружок «идёт» в названии сменяется
        отметкой готовности; без открытого шага закрывать нечего."""
        step = self._turn.seal_thinking()
        if step is None:
            return

        step.name = StepStatus.DONE.title(self.label(StepText.THINKING))
        step.end = utc_now()
        await self._sink.put(step)

    async def _open_thinking(self, key: str | None) -> Step:
        """Шаг не уходит в sink сразу: его откроет первый же stream_token."""
        step = await self._child(
            StepStatus.IDLE.title(self.label(StepText.THINKING)),
            StepKind.LLM,
            key,
            StepRole.THINKING,
        )
        step.start = utc_now()

        return step

    async def tool_started(
        self,
        name: str,
        args: Mapping[str, Any] | None,
        key: str | None,
        media: Mapping[str, str] | None,
        app: ToolAppView | None,
    ) -> Step:
        """Шаг начатого вызова. name — имя инструмента у модели: у
        инструмента сервера MCP оно полное, а шаг подписан именем без
        сервера. media — типы содержимого аргументов инструмента сервера
        (имя → contentMediaType): по ним рисуется вход шага; None —
        инструмент свой либо запись истории их не несёт, и вход рисует
        модель вызова инструмента, а без неё — json-текст. app — приложение
        MCP Apps инструмента: его карточка встаёт под контейнером хода сразу
        с аргументами; None — итог рисуется только блоками."""
        await self.seal_answer()
        shown = self._names.tool_of(name)

        call_args: Mapping[str, Any] = {}
        intent = ""
        if args:
            call_args = ToolIntent.without(args)
            intent = ToolIntent.of(args)

        label = ToolStepLabel.of(shown, intent)

        step = await self._child(
            StepStatus.IDLE.title(label), StepKind.TOOL, key, StepRole.TOOL
        )
        self._tool_names[step.id] = label

        if call_args:
            markdown = self._call_of(shown, call_args, media).markdown()
            if markdown:
                step.input = markdown
                step.show_input = True

        # фронт chainlit рисует секцию output (с inline-элементами, в том числе
        # кнопкой журнала) только при непустом output шага
        step.output = self.label(StepText.RUNNING)
        step.start = utc_now()
        await self._sink.put(step)

        if card := await self._app_card(call_args, key, app):
            self._awaiting[step.id] = card

        return step

    def _call_of(
        self, name: str, args: Mapping[str, Any], media: Mapping[str, str] | None
    ) -> ToolCallBase:
        if media is None:
            return ToolCallModels.call_of(name, args)

        return self._call_views.call_of(name, media, args)

    async def _app_card(
        self, args: Mapping[str, Any], key: str | None, app: ToolAppView | None
    ) -> AppCard | None:
        """Карточка приложения MCP Apps вызова с ключом key — своё сообщение
        основной ленты под контейнером хода с элементом ToolApp, сразу с
        аргументами вызова args; None — у инструмента приложения нет либо
        лента не рисует элементов."""
        if app is None:
            return None

        if not self._sink.emits_elements:
            return None

        if not key:
            return None

        element = self._elements.app(app, key, args)
        element.thread_id = self._thread_id
        if element_id := self.derive_id(self._thread_id, key, StepRole.APP):
            element.id = element_id

        parent = await self._parent()
        message = self._step(
            self._assistant_name,
            StepKind.ASSISTANT,
            parent_id=parent.id,
            step_id=self.derive_id(self._thread_id, key, StepRole.APP_MESSAGE),
        )
        message.output = ""
        message.metadata = {StepMeta.APP.value: True}
        message.elements = [element]
        await self._sink.put(message)

        return AppCard(message=message, element=element)

    async def _app_settled(self, step: Step, prop: AppProp, value: object) -> None:
        """Исход вызова шага step — карточке его приложения: итог tools/call
        (AppProp.RESULT) либо причина отмены (AppProp.CANCELLED); у вызова
        без карточки ничего."""
        card = self._awaiting.pop(step.id, None)
        if card is None:
            return

        card.element.props[prop.value] = value
        await self._sink.put(card.message)

    async def question_asked(self, card: QuestionCard, key: str) -> Step:
        """Вопрос агента пользователю — своим сообщением основной ленты на
        месте хода: пока вопрос ждёт ответа, элемент Question рисует строку
        с мигающей точкой (сама панель — у поля ввода вкладки), кружок
        ожидания хода на это время снят. Запись ответа встаёт в то же
        сообщение (question_finished)."""
        await self.seal_answer()
        step = self._question_step(key)
        if self._sink.emits_elements:
            step.elements = [self._question(card, key)]

        await self._sink.put(step)
        await self._pulse.hold()

        return step

    async def question_finished(self, step: Step, card: QuestionCard, key: str) -> None:
        """Запись ответа на вопрос в его сообщении: та же панель только для
        чтения с выбранными вариантами; кружок ожидания хода возвращается."""
        if self._sink.emits_elements:
            step.elements = [self._question(card, key)]

        await self._sink.put(step)
        await self._pulse.resume()

    async def questions_answered(self, cards: Sequence[QuestionCard], key: str) -> None:
        """Записи ответов на вопросы сервера MCP, заданные в вызове key, —
        сообщениями основной ленты рядом с итогом вызова."""
        await self.seal_answer()
        for number, card in enumerate(cards):
            asked = f"{key}#{number}"
            step = self._question_step(asked)
            if self._sink.emits_elements:
                step.elements = [self._question(card, asked)]

            await self._sink.put(step)

        await self._pulse.bump()

    def _question_step(self, key: str) -> Step:
        step = self._step(
            self._assistant_name,
            StepKind.ASSISTANT,
            parent_id=None,
            step_id=self.derive_id(self._thread_id, key, StepRole.QUESTION),
        )
        step.output = ""

        return step

    def _question(self, card: QuestionCard, key: str) -> CustomElement:
        element = self._elements.question(card.props())
        element.thread_id = self._thread_id
        element_id = self.derive_id(self._thread_id, key, StepRole.QUESTION_CARD)
        if element_id:
            element.id = element_id

        return element

    async def tool_journaled(self, step: Step, name: str, key: str) -> None:
        """Кнопка журнала вызова инструмента name с ключом key у шага step:
        вживую — с первым уведомлением о журнале, у итога и при
        воспроизведении — по ссылке журнала из ответа. Повторный вызов
        кнопку не дублирует; шаг уходит в ленту, только если кнопка
        добавилась."""
        if not self._sink.emits_elements:
            return

        button = self._stream_button(name, key)

        elements: list[Element] = []
        if step.elements:
            elements = list(step.elements)

        for element in elements:
            if element.id == button.id:
                return

        elements.append(button)
        step.elements = elements
        await self._sink.put(step)

    def _stream_button(self, name: str, key: str) -> CustomElement:
        """Кнопка журнала вызова; подпись — имя инструмента без сервера."""
        element = CustomElement(
            name=CanvasElement.STREAM.value,
            props={
                str(CanvasAction.CALL_ID): key,
                "label": self._names.tool_of(name),
            },
            thread_id=self._thread_id,
        )
        element_id = self.derive_id(self._thread_id, key, StepRole.STREAM)
        if element_id:
            element.id = element_id

        return element

    async def tool_progressed(self, step: Step, text: str) -> None:
        """Ход работы идущего вызова: блок шага показывает последний отчёт
        инструмента вместо общей подписи «running»."""
        flow = self._progress.get(step.id)
        if flow is None:
            flow = ProgressText()
            self._progress[step.id] = flow

        step.output = flow.shown(text)
        await self._sink.put(step)

    async def tool_finished(
        self,
        step: Step,
        reply: object,
        tool_call_id: str | None,
        elapsed_ms: int,
    ) -> None:
        """Итог вызова шагу step. Ответ сервера MCP (CallToolResult)
        рисуется блоками для человека по их виду и MIME, ✖ — по isError;
        elapsed_ms — длительность вызова, 0 — не измерена. Любой другой
        rawOutput (итог старого треда, ответ чужого агента) показывается
        своим JSON."""
        # завершённость шага = start == end; иначе фронт считает его живым
        ended = utc_now()
        step.start = ended
        step.end = ended
        self._progress.pop(step.id, None)
        label = self._tool_names.get(step.id, step.name)
        if not isinstance(reply, mt.CallToolResult):
            step.output = self._raw_output(reply)
            step.language = self._raw_language(reply)
            step.name = StepStatus.DONE.title(label)
            await self._sink.put(step)
            await self._app_settled(step, AppProp.CANCELLED, AppCancel.NO_RESULT.value)
            return

        status = StepStatus.DONE
        if reply.is_error:
            status = StepStatus.FAILED

        step.name = status.timed(label, self._elapsed.of(elapsed_ms))
        step.is_error = reply.is_error
        shown: list[str] = []
        async for piece in self._pieces(reply, step, tool_call_id):
            shown.append(piece)

        step.output = "\n\n".join(shown)
        await self._sink.put(step)

        result = reply.model_dump(mode="json", by_alias=True, exclude_none=True)
        await self._app_settled(step, AppProp.RESULT, result)

    async def _pieces(
        self, reply: mt.CallToolResult, step: Step, key: str | None
    ) -> AsyncIterator[str]:
        """Непустой markdown блоков ответа для человека по порядку: блок
        только для модели не показывается; номер — порядковый номер
        блока-не-текста, из него id элемента."""
        number = 0
        for block in reply.content:
            annotations = block.annotations
            if annotations is not None:
                if not BlockAudience.USER.reads(annotations.audience):
                    continue

            drawing = self._drawings.get(type(block), self._unknown)
            piece = await drawing(block, step, key, number)

            if not isinstance(block, mt.TextContent):
                number += 1

            if not piece:
                continue

            yield piece

    async def _text(
        self, block: mt.ContentBlock, step: Step, key: str | None, number: int
    ) -> str:
        if not isinstance(block, mt.TextContent):
            return await self._unknown(block, step, key, number)

        return block.text

    async def _embedded(
        self, block: mt.ContentBlock, step: Step, key: str | None, number: int
    ) -> str:
        """Встроенный ресурс по MIME; двоичный и неизвестного вида — JSON."""
        if not isinstance(block, mt.EmbeddedResource):
            return await self._unknown(block, step, key, number)

        resource = block.resource
        if not isinstance(resource, mt.TextResourceContents):
            return await self._unknown(block, step, key, number)

        mime = resource.mime_type
        if mime is None:
            mime = ContentMime.PLAIN.value

        drawing = self._resources.get(mime)
        if drawing is None and mime.startswith(self.TEXT_FAMILY):
            drawing = self._fenced

        if drawing is None:
            return await self._unknown(block, step, key, number)

        segment = PurePosixPath(urlsplit(str(resource.uri)).path).name
        name = unquote(segment)

        return await drawing(resource.text, name, step, key, number)

    async def _table(
        self, text: str, name: str, step: Step, key: str | None, number: int
    ) -> str:
        """text/csv — таблица markdown, как у таблиц семейства."""
        rows = list(csv.DictReader(io.StringIO(text)))
        if not rows:
            return NoteLine.render(self.label(StepText.NO_ROWS))

        return TableText.render(rows)

    async def _fenced(
        self, text: str, name: str, step: Step, key: str | None, number: int
    ) -> str:
        """Текст блоком кода; подпись ограды — имя ресурса."""
        return Fence.around(text, name)

    async def _chart(
        self, text: str, name: str, step: Step, key: str | None, number: int
    ) -> str:
        """Фигура plotly — график шагом рядом с вызовом; заголовок — имя
        ресурса. Фигура не JSON — текстом."""
        try:
            figure = json.loads(text)
        except ValueError as exc:
            logger.info(
                "thread %s: plotly figure of call %s is not JSON, shown as text: %s",
                self._thread_id,
                key,
                exc,
            )
            return await self._fenced(text, name, step, key, number)

        await self._element(figure, name, key)

        return ""

    async def _tree(
        self, text: str, name: str, step: Step, key: str | None, number: int
    ) -> str:
        """application/json — дерево JSON элементом шага вызова под его
        выходом; больше TREE_BYTES или не JSON — блоком кода."""
        if len(text.encode()) > self.TREE_BYTES:
            return await self._fenced(text, name, step, key, number)

        try:
            value = json.loads(text)
        except ValueError as exc:
            logger.info(
                "thread %s: JSON resource %r of call %s is not JSON, shown as text: %s",
                self._thread_id,
                name,
                key,
                exc,
            )
            return await self._fenced(text, name, step, key, number)

        if not self._sink.emits_elements:
            return ""

        tree = VisualResult(
            element=CanvasElement.JSON_TREE, props={"value": value}, title=name
        )
        element = self._elements.of_result(tree)
        self._numbered(element, key, number)

        elements: list[Element] = []
        if step.elements:
            elements = list(step.elements)

        elements.append(element)
        step.elements = elements

        return ""

    async def _media(
        self, block: mt.ContentBlock, step: Step, key: str | None, number: int
    ) -> str:
        """Картинка и звук — файл в хранилище треда под именем от шага вызова
        и номера блока: вживую, при воспроизведении и во второй вкладке это
        один файл, записанный один раз. Дальше он рисуется как файл
        workspace (_mounted). Не base64 — JSON блока без байтов."""
        if not isinstance(block, mt.ImageContent | mt.AudioContent):
            return await self._unknown(block, step, key, number)

        try:
            # проверка срезами до записи: битый блок не оставит в хранилище обрывка
            async for _ in self._decoded(block.data):
                pass
        except binascii.Error as exc:
            logger.info(
                "thread %s: %s of call %s is not base64, shown as JSON: %s",
                self._thread_id,
                block.type,
                key,
                exc,
            )
            return await self._unknown(block, step, key, number)

        if not self._sink.emits_elements:
            return ""

        suffix = ContentMime.suffix_of(block.mime_type)

        stem = uuid5(self.NAMESPACE, f"{step.id}:{number}")
        try:
            scope = await self._scope()
        except StorageError as exc:
            return f"{stem}{suffix} is not shown: {exc}"

        file_key = scope.file(f"{stem}{suffix}")
        try:
            await self._kept(file_key.render(), block)
        except StorageError as exc:
            return f"{file_key.in_workspace()} is not shown: {exc}"

        mounted = WorkspaceFile(path=file_key.in_workspace(), mime=block.mime_type)

        return await self._mounted(mounted, file_key.name, key, number)

    async def _kept(
        self, target: str, block: mt.ImageContent | mt.AudioContent
    ) -> None:
        """Байты блока — в хранилище под ключом target, если их там ещё нет.

        Ошибки:
        StorageError — хранилище не прочитало или не записало файл.
        """
        try:
            await self._storage.stat(target)
        except StorageNotFoundError:
            chunks = self._decoded(block.data)
            await self._storage.upload_stream(target, chunks, block.mime_type)

    async def _decoded(self, data: str) -> AsyncIterator[bytes]:
        """Байты base64 срезами BASE64_SLICE: в памяти — один срез.

        Ошибки:
        binascii.Error — срез не base64.
        """
        for start in range(0, len(data), self.BASE64_SLICE):
            piece = data[start : start + self.BASE64_SLICE]
            yield base64.b64decode(piece, validate=True)

    async def _linked(
        self, block: mt.ContentBlock, step: Step, key: str | None, number: int
    ) -> str:
        """Ссылка: файл workspace (file://) — вьювер канваса либо вложение у
        ответа хода; http(s) — вложение по адресу; другая схема — JSON."""
        if not isinstance(block, mt.ResourceLink):
            return await self._unknown(block, step, key, number)

        if mounted := self._links.file_of(block):
            return await self._mounted(mounted, block.name, key, number)

        address = urlsplit(str(block.uri))
        if address.scheme not in set(UrlScheme):
            return await self._unknown(block, step, key, number)

        answer_id = self.derive_id(self._thread_id, self._turn.key, StepRole.ANSWER)
        if answer_id is None:
            return ""

        if not self._sink.emits_elements:
            return ""

        attached = self._file(block.name, str(block.uri), block.mime_type, key, number)
        await attached.send(for_id=answer_id)

        return ""

    async def _unknown(
        self, block: mt.ContentBlock, step: Step, key: str | None, number: int
    ) -> str:
        """Неизвестный блок — его JSON; байты blob и data не раскрываются."""
        shown = block.model_dump(
            mode="json", by_alias=True, exclude_none=True, exclude=self.BYTES
        )

        return Fence.around(JsonBlock.pretty(shown), StepLanguage.JSON.value)

    async def _scope(self) -> CanvasScope:
        """Файлы треда — в workspace сервера файлов треда: каталог даёт
        хранилище.

        Ошибки:
        StorageError — хранилище каталога не знает.
        """
        workspace = await self._storage.workspace(self._user_key, self._thread_id)

        return CanvasScope(
            user_id=self._user_key, thread_id=self._thread_id, workspace=workspace
        )

    async def _mounted(
        self, mounted: WorkspaceFile, name: str, key: str | None, number: int
    ) -> str:
        """Файл workspace по ссылке — элемент у ответа хода, одинаково вживую
        и при воспроизведении: файл с вьювером канваса — его карточкой,
        остальное — вложением. Итог — пояснение для выхода шага вызова,
        когда элемента нет (путь вне треда, вьювер отказал)."""
        answer_id = self.derive_id(self._thread_id, self._turn.key, StepRole.ANSWER)
        if answer_id is None:
            return ""

        if not self._sink.emits_elements:
            return ""

        try:
            scope = await self._scope()
        except StorageError as exc:
            return f"{mounted.path} is not shown: {exc}"

        try:
            file_key = scope.key(mounted.path)
        except CanvasError as exc:
            return f"{mounted.path} is not shown: {exc}"

        mime = mounted.mime
        element: Element
        viewer = CanvasRegistry.viewer_for(mime)
        if viewer is None:
            url = CanvasFileUrl.path(file_key, mime)
            element = self._file(name, url, mime, key, number)
        else:
            try:
                card = await viewer.link(file_key, mime)
            except CanvasError as exc:
                return f"{file_key.in_workspace()} is not shown: {exc}"

            element = self._elements.of_result(card)
            self._numbered(element, key, number)

        await element.send(for_id=answer_id)

        return ""

    def _file(
        self, name: str, url: str, mime: str | None, key: str | None, number: int
    ) -> File:
        """Файл в ленте — вложение вопроса или файл итога вызова."""
        attached = File(name=name, url=url, mime=mime, display="inline")
        self._numbered(attached, key, number)

        return attached

    def _numbered(self, element: Element, key: str | None, number: int) -> None:
        """Id элемента из ключа вопроса или вызова и номера элемента у него —
        одинаковый вживую и при воспроизведении; без ключа элемент
        остаётся со своим id, чтобы не совпасть с элементом другого хода."""
        if not key:
            return

        element_id = self.derive_id(
            self._thread_id, f"{key}/{number}", StepRole.ELEMENT
        )
        if element_id is None:
            return

        element.id = element_id

    async def tool_stopped(self, step: Step) -> None:
        """Инструмент не доработал: ход кончился раньше вызова, приложение
        вызова получает отмену."""
        self._progress.pop(step.id, None)
        step.name = StepStatus.STOPPED.title(self._tool_names.get(step.id, step.name))
        step.output = self.label(StepText.FINISHED)
        ended = utc_now()
        step.start = ended
        step.end = ended
        await self._sink.put(step)
        await self._app_settled(step, AppProp.CANCELLED, AppCancel.STOPPED.value)

    @staticmethod
    def _raw_output(artifact: object) -> dict[str, str] | str:
        """Выход шага для записи старой истории без результата семейства:
        текст показывается как есть, остальное — своим JSON."""
        if artifact is None:
            return {}

        if isinstance(artifact, str):
            return {"content": artifact}

        return json.dumps(artifact, default=str)

    @staticmethod
    def _raw_language(artifact: object) -> str | None:
        """Язык подсветки выхода записи без результата семейства."""
        if artifact is None:
            return None

        if isinstance(artifact, str):
            return StepLanguage.TEXT.value

        return StepLanguage.JSON.value

    async def tool_failed(self, step: Step, markdown: str) -> None:
        """Провал инструмента мимо результата: markdown результата-ошибки;
        приложение вызова получает отмену с этим текстом."""
        step.is_error = True
        step.name = StepStatus.FAILED.title(self._tool_names.get(step.id, step.name))
        step.output = markdown
        ended = utc_now()
        step.start = ended
        step.end = ended
        await self._sink.put(step)
        await self._app_settled(step, AppProp.CANCELLED, markdown)

    async def _element(
        self, figure: Mapping[str, Any], title: str, tool_call_id: str | None
    ) -> None:
        """Шаг-носитель графика plotly рядом с шагом вызова."""
        step = self._step(
            self._assistant_name,
            StepKind.ASSISTANT,
            parent_id=None,
            step_id=self.derive_id(self._thread_id, tool_call_id, StepRole.CHART),
        )

        step.output = title

        if self._sink.emits_elements:
            element = self._elements.plotly(figure, title)
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
        self._turn.steps += 1
        return self._step(
            name,
            kind,
            parent_id=parent.id,
            step_id=self.derive_id(self._thread_id, key, role),
        )

    async def _parent(self) -> Step:
        """Родитель очередного шага: контейнер хода."""
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
