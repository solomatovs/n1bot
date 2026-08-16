"""Tool diagram_save и вьювер .mmd для канваса: спека mermaid файлом в
workspace, показ — панелью канваса.

Ошибки: ErrorResult — нет сессии, битая спека, путь вне каталогов треда,
файл не найден, не отдан хранилищем или не текст; остальное упаковывает
ToolErrorGuard. Вотчер канваса — фоновая задача: сбой доставки логируется
и завершает слежение.
"""

from __future__ import annotations

import asyncio
import logging
import textwrap
import uuid
from collections.abc import Awaitable, Callable
from enum import StrEnum
from typing import Annotated, ClassVar, Self

from langchain_core.tools import BaseTool, InjectedToolCallId, tool
from pydantic import BaseModel, ConfigDict, Field

import chainlit as cl
from boba.chainlit.data.data_layer import AttachmentDataLayer
from boba.chainlit.data.storage import StorageError, StorageNotFoundError
from boba.chainlit.domain.keys import ObjectKey, ThreadDir
from boba.chainlit.domain.session import current_thread_id, current_user_id
from boba.chainlit.domain.turn import ActiveTurns
from boba.chainlit.rendering.canvas import (
    CanvasContent,
    CanvasError,
    CanvasErrorKind,
    CanvasKind,
    CanvasPanel,
    CanvasPush,
    CanvasRegistry,
    OpenedCanvas,
    RenderStatus,
    RenderVerdicts,
)
from boba.chainlit.rendering.chat_view import ChatView, StepRole
from boba.toolkit.result import (
    DiagramResult,
    ErrorResult,
    TextResult,
    ToolResult,
    pack_result,
)
from boba.workspace.launcher import ReadWindow
from chainlit.data import get_data_layer

__all__ = [
    "CanvasWatcher",
    "DiagramCard",
    "DiagramEntry",
    "DiagramErrorKind",
    "DiagramFiles",
    "DiagramMarker",
    "DiagramPrompt",
    "DiagramRefusedError",
    "DiagramSpecError",
    "DiagramToolConfig",
    "MermaidSpec",
    "MermaidToken",
    "MermaidViewer",
    "build_diagram_tools",
]

logger = logging.getLogger(__name__)


class DiagramSpecError(ValueError):
    """Спека пуста или не начинается с известного типа диаграммы mermaid."""


class DiagramErrorKind(StrEnum):
    """Коды отказов тулов диаграмм: уезжают в ErrorResult.error_kind."""

    NO_SESSION = "no_session"
    NO_THREAD = "no_thread"
    NO_TURN = "no_turn"
    NO_TOOL_CALL = "no_tool_call"
    INVALID_SPEC = "invalid_diagram_spec"
    BAD_PATH = "bad_path"
    FILE_NOT_FOUND = "file_not_found"
    STORAGE_ERROR = "storage_error"
    BAD_FILE = "bad_file"


class DiagramRefusedError(Exception):
    """Тул отработать не может; текст причины готов для LLM."""

    def __init__(self, kind: DiagramErrorKind, message: str) -> None:
        super().__init__(message)
        self.kind = kind


class DiagramToolConfig(BaseModel):
    """Секция [tool.diagram]: предел размера спеки."""

    model_config = ConfigDict(extra="ignore")

    max_chars: int = Field(ge=1)


class MermaidToken(StrEnum):
    """Маркеры текста спеки, которые понимает нормализация."""

    FENCE = "```"
    FRONTMATTER = "---"
    COMMENT = "%%"
    TITLE_KEY = "title:"


class DiagramMarker(StrEnum):
    """Служебные имена: jsx-компонент, mime файла, канвас, fallback-имя."""

    MIME = "text/plain"
    FALLBACK_NAME = "diagram.mmd"
    SUFFIX = ".mmd"


class DiagramPrompt(StrEnum):
    """Тексты фасада для LLM: описания параметров и оговорка о рендере."""

    NAME = (
        "Имя файла диаграммы с расширением .mmd, например 'orders.mmd'. "
        "Файл ляжет в '/workspace/<thread_id>/mermaid/'."
    )
    SPEC = (
        "Спека mermaid. Первая строка — тип диаграммы: erDiagram, flowchart, "
        "sequenceDiagram, stateDiagram, gantt, mindmap и другие. Направление "
        "задаётся строкой 'direction LR' внутри спеки. Большую схему дроби на "
        "несколько диаграмм. Синтаксис тела проверяет браузер при показе, "
        "поэтому пиши строго: подпись подграфа без пробелов внутри скобок — "
        "'subgraph ID[\"Текст\"]', а не 'subgraph ID[ \"Текст\" ]'. "
        "sankey-beta принимает в подписях узлов только латиницу — для русских "
        "подписей бери другой тип (flowchart, xychart-beta)."
    )
    SAVED_NOTE = (
        "the diagram is rendered in the canvas panel and its card is shown "
        "in the chat. A render failure comes back to you as a tool error."
    )


class MermaidSpec(BaseModel):
    """Нормализованная спека mermaid: текст, тип диаграммы, заголовок."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    text: str
    diagram_type: str
    title: str | None

    TYPES: ClassVar[frozenset[str]] = frozenset(
        {
            "flowchart",
            "graph",
            "sequenceDiagram",
            "classDiagram",
            "stateDiagram",
            "stateDiagram-v2",
            "erDiagram",
            "journey",
            "gantt",
            "pie",
            "quadrantChart",
            "requirementDiagram",
            "gitGraph",
            "C4Context",
            "C4Container",
            "C4Component",
            "C4Dynamic",
            "C4Deployment",
            "mindmap",
            "timeline",
            "kanban",
            "block-beta",
            "packet-beta",
            "sankey-beta",
            "xychart-beta",
            "architecture-beta",
            "radar-beta",
            "treemap-beta",
        }
    )
    """Типы диаграмм mermaid 11; пересматривается при обновлении версии в Makefile."""

    @classmethod
    def parse(cls, raw: str) -> Self:
        """Разобрать спеку; неразбираемый вход — DiagramSpecError."""
        text = cls._strip_fence(raw)

        if not text:
            raise DiagramSpecError("the spec is empty")

        title, body = cls._split_frontmatter(text)

        head = cls._first_token(body)
        if head is None:
            raise DiagramSpecError("the spec has no meaningful lines")

        if head not in cls.TYPES:
            known = ", ".join(sorted(cls.TYPES))
            raise DiagramSpecError(
                f"the first line must name a mermaid diagram type, "
                f"got {head!r}; supported: {known}"
            )

        return cls(text=text, diagram_type=head, title=title)

    @classmethod
    def _strip_fence(cls, raw: str) -> str:
        text = textwrap.dedent(raw).strip()

        lines = text.splitlines()
        if lines and lines[0].startswith(MermaidToken.FENCE):
            lines = lines[1:]
        if lines and lines[-1].strip() == MermaidToken.FENCE:
            lines = lines[:-1]

        return textwrap.dedent("\n".join(lines)).strip()

    @classmethod
    def _split_frontmatter(cls, text: str) -> tuple[str | None, str]:
        """Заголовок из YAML-frontmatter и тело после него; сам блок не вырезается."""
        lines = text.splitlines()

        if not lines:
            return None, text

        if lines[0].strip() != MermaidToken.FRONTMATTER:
            return None, text

        title: str | None = None
        for index, line in enumerate(lines[1:], start=1):
            stripped = line.strip()
            if stripped == MermaidToken.FRONTMATTER:
                body = "\n".join(lines[index + 1 :])
                return title, body
            if stripped.startswith(MermaidToken.TITLE_KEY):
                title = cls._clean_title(stripped[len(MermaidToken.TITLE_KEY) :])

        return None, text

    @staticmethod
    def _clean_title(raw: str) -> str | None:
        cleaned = raw.strip().strip("'\"").strip()
        if not cleaned:
            return None
        return cleaned

    @classmethod
    def _first_token(cls, body: str) -> str | None:
        for line in body.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.startswith(MermaidToken.COMMENT):
                continue
            return stripped.split()[0]
        return None


class DiagramEntry(BaseModel):
    """Одна диаграмма для фронта: путь, подпись и текст спеки как есть."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str
    name: str
    label: str
    """Подпись в селекторе: title из спеки, иначе имя файла."""
    spec: str
    type: str
    """Тип диаграммы; пустой — спека сейчас не парсится."""

    @classmethod
    def of(cls, key: ObjectKey, text: str) -> Self:
        """Метаданные берутся из спеки; неразбираемая спека едет без них."""
        label = key.name
        diagram_type = ""

        try:
            parsed = MermaidSpec.parse(text)
        except DiagramSpecError:
            return cls(
                path=key.in_workspace(),
                name=key.name,
                label=label,
                spec=text,
                type=diagram_type,
            )

        if parsed.title:
            label = parsed.title

        return cls(
            path=key.in_workspace(),
            name=key.name,
            label=label,
            spec=parsed.text,
            type=parsed.diagram_type,
        )


class CanvasWatcher:
    """Слежение за файлом спеки: толкает изменения в канвас до конца хода.

    Живёт, пока alive() истинно. Ошибка чтения пропускает тик — файл в этот
    момент может переписываться; сбой доставки логируется и завершает задачу.
    """

    _TASKS: ClassVar[set[asyncio.Task[None]]] = set()
    """Живые вотчеры: без ссылки задачу заберёт сборщик мусора."""

    def __init__(
        self,
        read: Callable[[], Awaitable[str]],
        alive: Callable[[], bool],
        push: Callable[[str], Awaitable[None]],
        interval_sec: float,
    ) -> None:
        self._read = read
        self._alive = alive
        self._push = push
        self._interval_sec = interval_sec

    def spawn(self, initial: str) -> asyncio.Task[None]:
        task = asyncio.create_task(self.run(initial))
        self._TASKS.add(task)
        task.add_done_callback(self._TASKS.discard)
        return task

    async def run(self, initial: str) -> None:
        last = initial

        while self._alive():
            await asyncio.sleep(self._interval_sec)

            try:
                text = await self._read()
            except DiagramRefusedError:
                continue

            if text == last:
                continue

            last = text
            try:
                await self._push(text)
            except Exception:
                logger.warning("canvas watcher stopped: push failed", exc_info=True)
                return


class DiagramFiles:
    """Спеки mermaid в каталоге mermaid/ треда: проверка, сохранение, показ."""

    WATCH_INTERVAL_SEC: ClassVar[float] = 1.0

    def __init__(self, max_chars: int) -> None:
        self._max_chars = max_chars

    async def save(self, name: str, spec: str) -> ObjectKey:
        """Проверить спеку и записать файл; отказ — DiagramRefusedError."""
        user_id, thread_id = self._session()
        parsed = self._parse(spec)

        key = ObjectKey.build(
            user_id,
            thread_id,
            name,
            DiagramMarker.FALLBACK_NAME,
            dir_thread=ThreadDir.MERMAID,
        )

        await self._layer().storage.upload_file(
            object_key=key.render(),
            data=parsed.text,
            mime=DiagramMarker.MIME,
            overwrite=True,
        )

        return key

    def watch(
        self,
        key: ObjectKey,
        initial: str,
        push: Callable[[str], Awaitable[None]],
    ) -> None:
        """Слежение на время хода; вне хода (клик пользователя) вотчера нет."""
        thread_id = key.thread_id
        turn = ActiveTurns.of(thread_id)
        if turn is None:
            return

        async def read() -> str:
            return await self.read(key)

        def alive() -> bool:
            return ActiveTurns.of(thread_id) is turn

        watcher = CanvasWatcher(
            read=read,
            alive=alive,
            push=push,
            interval_sec=self.WATCH_INTERVAL_SEC,
        )
        watcher.spawn(initial)

    def _parse(self, spec: str) -> MermaidSpec:
        if len(spec) > self._max_chars:
            raise DiagramRefusedError(
                DiagramErrorKind.INVALID_SPEC,
                f"the spec is longer than the {self._max_chars} character limit",
            )

        try:
            return MermaidSpec.parse(spec)
        except DiagramSpecError as e:
            raise DiagramRefusedError(DiagramErrorKind.INVALID_SPEC, str(e)) from e

    @staticmethod
    def _session() -> tuple[str, str]:
        user_id = current_user_id()
        if not user_id:
            raise DiagramRefusedError(
                DiagramErrorKind.NO_SESSION, "no chainlit user session"
            )

        thread_id = current_thread_id()
        if not thread_id:
            raise DiagramRefusedError(DiagramErrorKind.NO_THREAD, "no active thread")

        return str(user_id), str(thread_id)

    @staticmethod
    def _key(user_id: str, thread_id: str, path: str) -> ObjectKey:
        try:
            return ObjectKey.from_workspace(user_id, thread_id, path)
        except ValueError as e:
            raise DiagramRefusedError(DiagramErrorKind.BAD_PATH, str(e)) from e

    UTF8_MAX_CHAR_BYTES: ClassVar[int] = 4
    """Потолок чтения в байтах: максимум utf-8 байт на символ лимита спеки."""

    async def read(self, key: ObjectKey) -> str:
        """Спека целиком в памяти: её размер ограничен, а хранилище лишь стримит."""
        try:
            blob = await self._collect(key)
        except StorageNotFoundError as e:
            raise DiagramRefusedError(
                DiagramErrorKind.FILE_NOT_FOUND,
                f"file not found: {key.in_workspace()}",
            ) from e
        except StorageError as e:
            raise DiagramRefusedError(
                DiagramErrorKind.STORAGE_ERROR,
                f"cannot read the file: {key.in_workspace()}: {e}",
            ) from e

        try:
            return blob.decode("utf-8")
        except UnicodeDecodeError as e:
            raise DiagramRefusedError(
                DiagramErrorKind.BAD_FILE,
                f"the file is not utf-8 text: {key.in_workspace()}",
            ) from e

    async def _collect(self, key: ObjectKey) -> bytes:
        """Читает файл потоком; слишком большой отвергается по размеру, до тела."""
        max_bytes = self._max_chars * self.UTF8_MAX_CHAR_BYTES
        storage = self._layer().storage

        async with await storage.open_stream(key.render(), ReadWindow.entire()) as body:
            if body.stat.size > max_bytes:
                raise DiagramRefusedError(
                    DiagramErrorKind.BAD_FILE,
                    f"the file is larger than the diagram limit: {key.in_workspace()}",
                )

            collected = bytearray()
            async for chunk in body.chunks:
                collected.extend(chunk)

        return bytes(collected)

    @staticmethod
    def _layer() -> AttachmentDataLayer:
        layer = get_data_layer()
        if not isinstance(layer, AttachmentDataLayer):
            msg = f"data layer does not address attachments: {type(layer)}"
            raise RuntimeError(msg)
        return layer


class MermaidViewer:
    """Вьювер канваса для .mmd: описывает диаграмму и ждёт вердикт рендера.

    Синтаксис спеки знает только mermaid.js в браузере, поэтому после показа
    вьювер ждёт canvas_render_status по nonce: FAILED — CanvasError с текстом
    ошибки mermaid, молчание браузера показу не мешает.
    """

    suffixes: ClassVar[frozenset[str]] = frozenset({DiagramMarker.SUFFIX})

    VERDICT_TIMEOUT_SEC: ClassVar[float] = 10.0

    def __init__(self, files: DiagramFiles) -> None:
        self._files = files

    def handles(self, name: str) -> bool:
        return name.lower().endswith(DiagramMarker.SUFFIX)

    async def content(self, key: ObjectKey) -> CanvasContent:
        return self._content(key, await self._read(key), str(uuid.uuid4()))

    async def open(self, key: ObjectKey, push: CanvasPush) -> OpenedCanvas:
        text = await self._read(key)
        nonce = str(uuid.uuid4())

        async def show(current: str) -> None:
            await push(self._content(key, current, nonce))

        RenderVerdicts.expect(nonce)
        await show(text)

        verdict = await RenderVerdicts.wait(nonce, self.VERDICT_TIMEOUT_SEC)
        if verdict.status is RenderStatus.FAILED:
            raise CanvasError(
                CanvasErrorKind.RENDER_FAILED,
                f"the diagram does not render in the browser: {verdict.message}",
            )

        self._files.watch(key, text, show)

        entry = DiagramEntry.of(key, text)
        link = DiagramResult(spec=text, path=entry.path, title=entry.label)

        return OpenedCanvas(label=entry.label, path=entry.path, link=link)

    async def _read(self, key: ObjectKey) -> str:
        try:
            return await self._files.read(key)
        except DiagramRefusedError as e:
            raise CanvasError(e.kind, str(e)) from e

    @staticmethod
    def _content(key: ObjectKey, text: str, nonce: str) -> CanvasContent:
        entry = DiagramEntry.of(key, text)

        return CanvasContent(
            kind=CanvasKind.MERMAID,
            path=entry.path,
            label=entry.label,
            text=entry.spec,
            nonce=nonce,
        )


class DiagramCard:
    """Карточка диаграммы в ленте: кликабельная ссылка на файл у шага ответа.

    Вердикт рендера с карточки не собирается: сообщение шага ответа появляется
    в DOM только с финальным ответом хода — во время инструмента карточке не на
    чем смонтироваться. Верификацию спеки делает показ в панели (diagram_save),
    поэтому карточка уходит без nonce и браузер по ней не отчитывается.
    """

    ELEMENT: ClassVar[str] = "CanvasView"

    def __init__(self, files: DiagramFiles) -> None:
        self._files = files

    async def publish(self, key: ObjectKey, tool_call_id: str) -> None:
        """Показать карточку в ленте; переживает перезагрузку треда."""
        text = await self._files.read(key)
        for_id, element_id = self._targets(key.thread_id, tool_call_id)

        await self._emit(key, text, for_id, element_id)

    @staticmethod
    def _targets(thread_id: str, tool_call_id: str) -> tuple[str, str]:
        turn = ActiveTurns.of(thread_id)
        if turn is None:
            raise DiagramRefusedError(
                DiagramErrorKind.NO_TURN, "the turn is already finished"
            )

        for_id = turn.answer_step_id
        if not for_id:
            raise DiagramRefusedError(
                DiagramErrorKind.NO_TURN, "the turn has no answer step"
            )

        element_id = ChatView.derive_id(thread_id, tool_call_id, StepRole.ELEMENT)
        if not element_id:
            raise DiagramRefusedError(
                DiagramErrorKind.NO_TOOL_CALL, "tool call without id"
            )

        return for_id, element_id

    async def _emit(
        self,
        key: ObjectKey,
        text: str,
        for_id: str,
        element_id: str,
    ) -> None:
        entry = DiagramEntry.of(key, text)
        content = CanvasContent(
            kind=CanvasKind.MERMAID,
            path=entry.path,
            label=entry.label,
            text=entry.spec,
        )
        props = {**content.props(), "preview": True}

        element = cl.CustomElement(name=self.ELEMENT, props=props)
        element.id = element_id
        element.thread_id = key.thread_id
        await element.send(for_id=for_id)


def build_diagram_tools(cfg: DiagramToolConfig) -> list[BaseTool]:
    files = DiagramFiles(cfg.max_chars)
    card = DiagramCard(files)
    # клик по карточке открывает файл в канвасе: вьювер знает про .mmd отсюда
    CanvasRegistry.register(MermaidViewer(files))

    @tool(response_format="content_and_artifact")
    async def diagram_save(
        name: Annotated[
            str,
            Field(min_length=1, description=DiagramPrompt.NAME),
        ],
        spec: Annotated[
            str,
            Field(min_length=1, description=DiagramPrompt.SPEC),
        ],
        tool_call_id: Annotated[str, InjectedToolCallId],
    ) -> tuple[str, ToolResult]:
        """Сохранить спеку mermaid файлом в workspace, показать её в панели
        канваса и оставить карточку в переписке.

        Спеку проверяет только mermaid.js в браузере, а во время хода
        смонтирована лишь панель — поэтому показ в ней и есть верификация:
        её вердикт возвращается LLM ошибкой инструмента.

        Карточка вешается на ответ только после удачного показа: неудачную
        LLM правит следующим вызовом, и в переписке ей места нет — попытка
        видна шагом инструмента внутри хода.
        """
        try:
            key = await files.save(name, spec)
        except DiagramRefusedError as e:
            return pack_result(ErrorResult(message=str(e), error_kind=e.kind))

        path = key.in_workspace()

        try:
            await CanvasPanel.open(key)
        except CanvasError as e:
            message = (
                f"diagram saved: {path}, but {e}; "
                "fix the spec and call diagram_save again"
            )
            return pack_result(ErrorResult(message=message, error_kind=e.kind))

        try:
            await card.publish(key, tool_call_id)
        except DiagramRefusedError as e:
            return pack_result(ErrorResult(message=str(e), error_kind=e.kind))

        return pack_result(
            TextResult(
                text=f"diagram saved: {path}; {DiagramPrompt.SAVED_NOTE}",
                metadata={"path": path},
            )
        )

    return [diagram_save]
