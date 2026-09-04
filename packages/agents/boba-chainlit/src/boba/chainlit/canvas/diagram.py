"""Tool diagram_save и вьювер .mmd для канваса: спека mermaid файлом в
workspace, показ — панелью канваса.

Ошибки: ErrorResult — нет сессии, битая спека, путь вне каталогов треда,
файл не найден, не отдан хранилищем или не текст; остальное упаковывает
ToolErrorGuard.
"""

from __future__ import annotations

import logging
import uuid
from typing import Annotated, ClassVar

from langchain_core.tools import BaseTool, tool
from pydantic import Field

import chainlit as cl
from boba.canvas.canvas import (
    CanvasContent,
    CanvasError,
    CanvasErrorKind,
    CanvasKind,
    CanvasPush,
    CanvasRegistry,
    OpenedCanvas,
    RenderStatus,
    RenderVerdicts,
    WatchSource,
)
from boba.canvas.diagram import (
    DiagramEntry,
    DiagramErrorKind,
    DiagramMarker,
    DiagramPrompt,
    DiagramRefusedError,
    DiagramSpecError,
    DiagramToolConfig,
    MermaidSpec,
)
from boba.canvas.keys import ObjectKey, ThreadDir
from boba.canvas.storage import StorageError, StorageNotFoundError
from boba.chainlit.canvas.panel import CanvasPanel, FileViewer, StorageHashSource
from boba.chainlit.data.data_layer import AttachmentDataLayer
from boba.chainlit.domain.context import ChatCallContext
from boba.identity.errors import RefusalError
from boba.identity.run import ElementTarget, RunPort, RunRegistry
from boba.toolkit.calls import ScriptCall, ToolCallViews
from boba.toolkit.result import (
    DiagramResult,
    ErrorResult,
    TextResult,
    ToolResult,
    pack_result,
)
from boba.workspace.launcher import ReadWindow

__all__ = [
    "DiagramCard",
    "DiagramFiles",
    "MermaidViewer",
    "build_diagram_tools",
]

logger = logging.getLogger(__name__)


class DiagramFiles:
    """Спеки mermaid в каталоге mermaid/ треда: проверка, сохранение, чтение."""

    def __init__(self, max_chars: int) -> None:
        self._max_chars = max_chars

    async def save(self, name: str, spec: str) -> ObjectKey:
        """Проверить спеку и записать файл; отказ — DiagramRefusedError."""
        user_id, thread_id = self._scope()
        parsed = self._parse(spec)

        key = ObjectKey.build(
            user_id,
            thread_id,
            name,
            DiagramMarker.FALLBACK_NAME,
            dir_thread=ThreadDir.MERMAID,
        )

        await AttachmentDataLayer.require().storage.upload_file(
            object_key=key.render(),
            data=parsed.text,
            mime=DiagramMarker.MIME,
            overwrite=True,
        )

        return key

    def _parse(self, spec: str) -> MermaidSpec:
        if len(spec) > self._max_chars:
            msg = (
                f"the spec is longer than the {self._max_chars} character limit: "
                f"got {len(spec)} characters"
            )
            raise DiagramRefusedError(DiagramErrorKind.INVALID_SPEC, msg)

        try:
            return MermaidSpec.parse(spec)
        except DiagramSpecError as e:
            raise DiagramRefusedError(DiagramErrorKind.INVALID_SPEC, str(e)) from e

    @staticmethod
    def _scope() -> tuple[str, str]:
        """Пользователь и тред области вызова; вне контекста — RefusalError."""
        context = ChatCallContext.require()
        return context.subject.user_key, context.scope.id

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
            msg = f"the file is not utf-8 text: {key.in_workspace()}: {e}"
            raise DiagramRefusedError(DiagramErrorKind.BAD_FILE, msg) from e

    async def _collect(self, key: ObjectKey) -> bytes:
        """Читает файл потоком; слишком большой отвергается по размеру, до тела."""
        max_bytes = self._max_chars * self.UTF8_MAX_CHAR_BYTES
        storage = AttachmentDataLayer.require().storage

        async with await storage.open_stream(key.render(), ReadWindow.entire()) as body:
            if body.stat.size > max_bytes:
                msg = (
                    f"the file is larger than the diagram limit of {max_bytes} "
                    f"bytes: {key.in_workspace()} has {body.stat.size} bytes"
                )
                raise DiagramRefusedError(DiagramErrorKind.BAD_FILE, msg)

            collected = bytearray()
            async for chunk in body.chunks:
                collected.extend(chunk)

        return bytes(collected)


class MermaidViewer(FileViewer):
    """Вьювер канваса для .mmd: описывает диаграмму и ждёт вердикт рендера.

    Наследует базу вьюверов: путь, подпись и ссылку на файл проставляет она,
    поэтому спеку можно скачать так же, как любой другой показанный файл.

    Синтаксис спеки знает только mermaid.js в браузере, поэтому после показа
    вьювер ждёт canvas_render_status по nonce: FAILED — CanvasError с текстом
    ошибки mermaid, молчание браузера показу не мешает.
    """

    kind: ClassVar[CanvasKind] = CanvasKind.MERMAID
    suffixes: ClassVar[frozenset[str]] = frozenset({DiagramMarker.SUFFIX})

    VERDICT_TIMEOUT_SEC: ClassVar[float] = 10.0

    def __init__(self, files: DiagramFiles) -> None:
        self._files = files

    async def content(self, key: ObjectKey) -> CanvasContent:
        return self._content(key, await self._read(key), str(uuid.uuid4()))

    async def open(self, key: ObjectKey, push: CanvasPush) -> OpenedCanvas:
        text = await self._read(key)
        nonce = str(uuid.uuid4())

        RenderVerdicts.expect(nonce)
        await push(self._content(key, text, nonce))

        verdict = await RenderVerdicts.wait(nonce, self.VERDICT_TIMEOUT_SEC)
        if verdict.status is RenderStatus.FAILED:
            raise CanvasError(
                CanvasErrorKind.RENDER_FAILED,
                f"the diagram does not render in the browser: {verdict.message}",
            )

        entry = DiagramEntry.of(key, text)
        link = DiagramResult(spec=text, path=entry.path, title=entry.label)

        return OpenedCanvas(label=entry.label, path=entry.path, nonce=nonce, link=link)

    def watch_source(self, key: ObjectKey) -> WatchSource | None:
        """Слежение по содержимому: спека мала, а размер может не меняться."""

        async def read() -> str:
            return await self._read(key)

        return StorageHashSource(read)

    async def _read(self, key: ObjectKey) -> str:
        try:
            return await self._files.read(key)
        except RefusalError as e:
            raise CanvasError(e.kind, str(e)) from e

    def _content(self, key: ObjectKey, text: str, nonce: str) -> CanvasContent:
        """Спека поверх описания базы: подпись берётся из заголовка диаграммы."""
        entry = DiagramEntry.of(key, text)

        described = self.describe(key, text=entry.spec)
        return described.model_copy(update={"label": entry.label, "nonce": nonce})


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

    async def publish(self, key: ObjectKey) -> None:
        """Показать карточку в ленте; переживает перезагрузку треда."""
        text = await self._files.read(key)
        context = ChatCallContext.require()
        port = RunRegistry.require_port(key.thread_id)
        call_id = context.tool_call_id()
        target = port.element_target(call_id)

        await self._emit(key, text, target, port, call_id)

    async def _emit(
        self,
        key: ObjectKey,
        text: str,
        target: ElementTarget,
        port: RunPort,
        call_id: str,
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
        element.id = target.element_id
        element.thread_id = key.thread_id
        element.for_id = target.for_id
        await AttachmentDataLayer.require().create_element(element)
        await port.show_element(call_id, element.to_dict())


def build_diagram_tools(cfg: DiagramToolConfig) -> list[BaseTool]:
    files = DiagramFiles(cfg.max_chars)
    card = DiagramCard(files)
    # клик по карточке открывает файл в канвасе: вьювер знает про .mmd отсюда
    CanvasRegistry.register(MermaidViewer(files))
    ToolCallViews.register("diagram_save", ScriptCall(arg="spec", lang="mermaid"))

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
        except RefusalError as e:
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
            await card.publish(key)
        except RefusalError as e:
            return pack_result(ErrorResult(message=str(e), error_kind=e.kind))

        return pack_result(
            TextResult(
                text=f"diagram saved: {path}; {DiagramPrompt.SAVED_NOTE}",
                metadata={"path": path},
            )
        )

    return [diagram_save]
