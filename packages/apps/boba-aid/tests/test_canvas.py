"""Канвас: реестр вьюверов, список файлов и отказы — общий слой без mermaid."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any, ClassVar
from urllib.parse import quote, urlsplit
from uuid import UUID

import chainlit as cl
import pytest
from aid_stand import ChatSessionStand, FakeTransport, RemoteStand, put_bytes

from boba.aid.canvas import panel as rendering_canvas
from boba.aid.canvas.diagram import MermaidViewer
from boba.aid.canvas.panel import CanvasPanel
from boba.aid.canvas.tools import (
    AudioViewer,
    CanvasOpener,
    CanvasViewers,
    ImageViewer,
    LogViewer,
    MarkdownViewer,
    PdfViewer,
    VideoViewer,
)
from boba.aid.infra.thread_room import ThreadSurfaces
from boba.canvas.canvas import (
    CanvasAction,
    CanvasContent,
    CanvasElement,
    CanvasError,
    CanvasErrorKind,
    CanvasFileRequest,
    CanvasKind,
    CanvasPush,
    CanvasRegistry,
    CanvasScope,
    CanvasViewer,
    CanvasWatch,
    OpenedCanvas,
)
from boba.canvas.keys import ObjectKey
from boba.runtime.config import AppLayers
from boba.runtime.storage import LocalStorageClient, LocalStorageConfig
from boba.stand_core.context import CallStand
from boba.toolkit.result import ContentMime, VisualResult
from boba.workspace.binaries import TrustedBinaries
from boba.workspace.launcher import MountingConfig

THREAD = "11111111-1111-1111-1111-111111111111"
MOUNT = "/workspace"
USER = str(UUID(int=7))

NO_REMOTE = RemoteStand().streams
"""Журналы MCP-серверов: в этих тестах серверов нет, вызовы только свои."""


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    """Заглушка сессионной фикстуры conftest: БД этим тестам не нужна."""


class FakeViewer(CanvasViewer):
    """Вьювер под тест: берётся за свой MIME и запоминает показанное."""

    kind: ClassVar[CanvasKind] = CanvasKind.MERMAID
    mimes: ClassVar[frozenset[str]] = frozenset({ContentMime.MERMAID})

    def __init__(self, mime: str) -> None:
        self._mime = mime
        self.opened: list[ObjectKey] = []

    def handles(self, mime: str) -> bool:
        return mime == self._mime

    async def content(self, key: ObjectKey, mime: str) -> CanvasContent:
        return CanvasContent(
            kind=CanvasKind.MERMAID,
            path=key.in_workspace(),
            label=key.name,
            text="erDiagram",
        )

    async def link(self, key: ObjectKey, mime: str) -> VisualResult:
        return VisualResult(
            element="CanvasLink",
            props={"path": key.in_workspace(), "label": key.name},
            title=key.name,
        )

    async def open(self, key: ObjectKey, mime: str, push: CanvasPush) -> OpenedCanvas:
        self.opened.append(key)
        await push(await self.content(key, mime))

        return OpenedCanvas(label=key.name, path=key.in_workspace(), nonce="n-1")

    def watch_source(self, key: ObjectKey) -> None:
        return None


class OtherViewer(FakeViewer):
    """Второй тип вьювера: реестр различает вьюверы по классу."""


class RewrittenAfterShow(ImageViewer):
    """Картинка, которую инструмент переписывает сразу после того, как её
    показ ушёл во фронт, — раньше первого опроса сторожа слежения."""

    def __init__(
        self, storage: LocalStorageClient, object_key: str, revised: bytes
    ) -> None:
        self._storage = storage
        self._object_key = object_key
        self._revised = revised

    async def open(self, key: ObjectKey, mime: str, push: CanvasPush) -> OpenedCanvas:
        opened = await super().open(key, mime, push)
        await put_bytes(self._storage, self._object_key, self._revised)

        return opened


@pytest.fixture(autouse=True)
def empty_registry() -> None:
    CanvasRegistry.reset()


@pytest.fixture
async def http_context() -> None:
    """ElementSidebar пишет в emitter сессии — без контекста панели не открыться."""
    from chainlit.context import init_http_context

    init_http_context()


class TestRegistry:
    def test_viewer_is_found_by_mime(self) -> None:
        viewer: CanvasViewer = FakeViewer(ContentMime.MERMAID)
        CanvasRegistry.register(viewer)

        if CanvasRegistry.viewer_for(ContentMime.MERMAID) is not viewer:
            raise AssertionError("CanvasRegistry.viewer_for(MERMAID) is viewer")
        if CanvasRegistry.viewer_for("application/pdf") is not None:
            raise AssertionError('CanvasRegistry.viewer_for("application/pdf") is None')

    def test_registration_is_idempotent_per_type(self) -> None:
        """Тулы собираются на каждую сессию — реестр не должен расти."""
        first = FakeViewer(ContentMime.MERMAID)
        second = FakeViewer(ContentMime.MERMAID)

        CanvasRegistry.register(first)
        CanvasRegistry.register(second)

        if CanvasRegistry.viewer_for(ContentMime.MERMAID) is not second:
            raise AssertionError("CanvasRegistry.viewer_for(MERMAID) is second")

    def test_different_types_live_together(self) -> None:
        first = FakeViewer(ContentMime.MERMAID)
        other = OtherViewer("application/pdf")
        CanvasRegistry.register(first)
        CanvasRegistry.register(other)

        if CanvasRegistry.viewer_for(ContentMime.MERMAID) is not first:
            raise AssertionError("CanvasRegistry.viewer_for(MERMAID) is first")
        if CanvasRegistry.viewer_for("application/pdf") is not other:
            raise AssertionError("CanvasRegistry.viewer_for(pdf) is other")

    def test_hint_without_viewers(self) -> None:
        if CanvasRegistry.mimes_hint() != "no viewers registered":
            raise AssertionError('CanvasRegistry.mimes_hint() == "no viewers regist…')


class TestPanel:
    @pytest.mark.anyio
    async def test_open_without_viewer_names_the_file(self, http_context: None) -> None:
        key = ObjectKey.build(USER, THREAD, "notes.txt", "el-1")

        with pytest.raises(CanvasError) as failure:
            await CanvasPanel.open(key, ContentMime.PLAIN)

        if failure.value.kind != CanvasErrorKind.NO_VIEWER:
            raise AssertionError("failure.value.kind == CanvasErrorKind.NO_VIEWER")
        if "notes.txt" not in str(failure.value):
            raise AssertionError('"notes.txt" in str(failure.value)')


class TestViewers:
    def test_register_all_covers_file_viewers(self) -> None:
        """PNG от bash/python-тулов обязан показываться — ход из бага."""
        CanvasRegistry.reset()
        CanvasViewers.register_all()

        expected: dict[str, type[CanvasViewer]] = {
            "график.png": ImageViewer,
            "report.PDF": PdfViewer,
            "notes.md": MarkdownViewer,
            "run.log": LogViewer,
            "notes.txt": LogViewer,
            "demo.mp4": VideoViewer,
            "voice.mp3": AudioViewer,
            "voice.ogg": AudioViewer,
            "shot.webp": ImageViewer,
            "orders.mmd": MermaidViewer,
        }
        for name, viewer_type in expected.items():
            viewer = CanvasRegistry.viewer_for(ContentMime.of_name(name))
            if not isinstance(viewer, viewer_type):
                raise AssertionError(f"{name}: {viewer!r} is {viewer_type.__name__}")

        if CanvasRegistry.viewer_for(ContentMime.of_name("data.bin")) is not None:
            raise AssertionError("data.bin has no viewer")
        if CanvasRegistry.viewer_for("text/plain; charset=utf-8") is None:
            raise AssertionError("MIME parameters do not hide the plain text viewer")
        if CanvasRegistry.viewer_for("text/csv") is not None:
            raise AssertionError("text/csv has no viewer")


class TestRefusal:
    """Отказ показа по клику: путь вне каталогов треда."""

    @pytest.mark.anyio
    async def test_path_outside_thread(self) -> None:
        scope = CanvasScope(user_id=USER, thread_id=THREAD, workspace=MOUNT)

        with pytest.raises(CanvasError) as failure:
            await CanvasOpener(ThreadSurfaces()).show(
                "/etc/passwd", ContentMime.PLAIN.value, scope
            )

        if failure.value.kind != CanvasErrorKind.BAD_PATH:
            raise AssertionError(failure.value.kind)


class _StorageOnlyLayer:
    """Доступ к слою в тесте: канвасу нужен только storage."""

    def __init__(self, storage: LocalStorageClient) -> None:
        self.storage = storage


@pytest.fixture
def storage(
    chat_session: ChatSessionStand,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> LocalStorageClient:
    config = LocalStorageConfig(
        files_dir=str(tmp_path),
        mounting=MountingConfig(
            mount_wait_sec=1.0,
            mount_poll_sec=0.1,
            shutdown_wait_sec=1.0,
            lock_wait_sec=1.0,
            copy_chunk_bytes=65536,
        ),
        mount_dir="/tmp",  # noqa: S108
        binaries=TrustedBinaries(dirs=("/usr/bin", "/bin")),
        interpreter=AppLayers.interpreter(),
    )
    client = LocalStorageClient(config)
    layer = _StorageOnlyLayer(client)

    chat_session.use(user_id=USER, thread_id=THREAD)
    monkeypatch.setattr(rendering_canvas, "get_data_layer", lambda: layer)

    return client


class TestShow:
    """Панель показывает один файл: тот же код у тула и клика по ссылке."""

    @pytest.mark.anyio
    async def test_show_opens_the_file(
        self, storage: LocalStorageClient, http_context: None
    ) -> None:
        viewer = FakeViewer(ContentMime.MERMAID)
        CanvasRegistry.register(viewer)
        await put_bytes(storage, f"{USER}/{THREAD}/mermaid/a.mmd", "erDiagram")

        opened = await CanvasOpener(ThreadSurfaces()).show(
            f"/workspace/{THREAD}/mermaid/a.mmd",
            ContentMime.MERMAID.value,
            CanvasScope(user_id=USER, thread_id=THREAD, workspace=MOUNT),
        )

        if opened.label != "a.mmd":
            raise AssertionError('opened.label == "a.mmd"')
        if viewer.opened[0].name != "a.mmd":
            raise AssertionError('viewer.opened[0].name == "a.mmd"')

    @pytest.mark.anyio
    async def test_panel_slot_id_is_stable(
        self,
        storage: LocalStorageClient,
        http_context: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Смена файла не пересоздаёт элемент во фронте — панель не мигает.

        Панель едет message-элементом с display='side': chainlit держит side
        view по таким элементам и не закрывает её на каждый новый ход.
        """
        CanvasRegistry.register(ImageViewer())
        png = b"\x89PNG\r\n\x1a\n" + bytes(8)
        await put_bytes(storage, f"{USER}/{THREAD}/upload/a.png", png)
        await put_bytes(storage, f"{USER}/{THREAD}/upload/b.png", png)

        shown: list[Any] = []
        titles: list[str] = []

        async def capture(self: Any, for_id: str | None = None) -> None:
            shown.append(self)

        class Sidebar:
            @staticmethod
            async def set_title(title: str) -> None:
                titles.append(title)

        monkeypatch.setattr(rendering_canvas.cl.CustomElement, "send", capture)
        monkeypatch.setattr(rendering_canvas.cl, "ElementSidebar", Sidebar)

        await CanvasOpener(ThreadSurfaces()).show(
            f"/workspace/{THREAD}/upload/a.png",
            "image/png",
            CanvasScope(user_id=USER, thread_id=THREAD, workspace=MOUNT),
        )
        await CanvasOpener(ThreadSurfaces()).show(
            f"/workspace/{THREAD}/upload/b.png",
            "image/png",
            CanvasScope(user_id=USER, thread_id=THREAD, workspace=MOUNT),
        )

        if [e.props["label"] for e in shown] != ["a.png", "b.png"]:
            raise AssertionError('[e.props["label"] for e in shown] == ["a.png", "b.p…')
        if [e.name for e in shown] != [CanvasElement.VIEW] * 2:
            raise AssertionError("[e.name for e in shown] == [CanvasElement.VIEW] * 2")
        if [e.display for e in shown] != ["side", "side"]:
            raise AssertionError('[e.display for e in shown] == ["side", "side"]')
        if shown[0].id != CanvasPanel.CONTENT_ID:
            raise AssertionError("shown[0].id == CanvasPanel.CONTENT_ID")
        if shown[1].id != CanvasPanel.CONTENT_ID:
            raise AssertionError("shown[1].id == CanvasPanel.CONTENT_ID")
        if titles != [CanvasPanel.TITLE, CanvasPanel.TITLE]:
            raise AssertionError("titles == [CanvasPanel.TITLE, CanvasPanel.TITLE]")

    @pytest.mark.anyio
    async def test_a_write_right_after_the_show_is_signaled(
        self,
        storage: LocalStorageClient,
        http_context: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Версия файла снимается до показа: правка, успевшая между показом и
        первым опросом сторожа, доходит до панели сигналом, а не становится
        исходной версией сторожа."""
        object_key = f"{USER}/{THREAD}/upload/a.png"
        png = b"\x89PNG\r\n\x1a\n" + bytes(8)
        revised = png + bytes(8)
        await put_bytes(storage, object_key, png)
        CanvasRegistry.register(RewrittenAfterShow(storage, object_key, revised))

        transport = FakeTransport()
        monkeypatch.setattr(
            rendering_canvas.PanelStorage, "client", staticmethod(lambda: storage)
        )
        monkeypatch.setattr(CanvasWatch, "POLL_SEC", 0.05)
        CanvasWatch.configure(transport)
        deadline = time.monotonic() + 5.0
        try:
            await CanvasOpener(ThreadSurfaces()).show(
                f"/workspace/{THREAD}/upload/a.png",
                "image/png",
                CanvasScope(user_id=USER, thread_id=THREAD, workspace=MOUNT),
            )
            while not transport.sent:
                if time.monotonic() > deadline:
                    raise AssertionError(
                        "no canvas signal within 5s for a write right after the show"
                    )
                await asyncio.sleep(0.01)
        finally:
            CanvasWatch.reset()

        if transport.sent[0]["size"] != len(revised):
            raise AssertionError(f"the signal names another version: {transport.sent}")


class ElementSink:
    """Приёмник push вьювера: тест смотрит, что уехало в панель."""

    def __init__(self) -> None:
        self.shown: list[CanvasContent] = []

    async def push(self, content: CanvasContent) -> None:
        self.shown.append(content)


class TestFileViewers:
    """Вьюверы содержимого: элемент со ссылкой на storage, а не тело в памяти."""

    PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16

    @pytest.mark.anyio
    async def test_image_viewer_links_to_storage(
        self, storage: LocalStorageClient, http_context: None
    ) -> None:
        """Тело файла не поднимается в память: элемент несёт ссылку на роут."""
        await put_bytes(storage, f"{USER}/{THREAD}/upload/график.png", self.PNG)
        key = ObjectKey.build(USER, THREAD, "график.png", "el-1")

        sink = ElementSink()
        opened = await ImageViewer().open(key, "image/png", sink.push)

        if opened.label != "график.png":
            raise AssertionError('opened.label == "график.png"')
        content = sink.shown[0]
        if content.kind is not CanvasKind.IMAGE:
            raise AssertionError("content.kind is CanvasKind.IMAGE")
        if content.label != "график.png":
            raise AssertionError('content.label == "график.png"')
        if content.mime != "image/png":
            raise AssertionError('content.mime == "image/png"')
        if f"/canvas/{THREAD}/upload/" not in content.url:
            raise AssertionError('f"/canvas/{THREAD}/upload/" in content.url')

    @pytest.mark.anyio
    async def test_link_outlives_the_session(
        self, storage: LocalStorageClient, http_context: None
    ) -> None:
        """Ссылка адресует файл, а не запись в памяти сессии.

        Содержимое панели рассылается во все вкладки треда и переживает
        переподключение; сессионная ссылка умирала раньше него, и картинка
        молча превращалась в битый img.
        """
        await put_bytes(storage, f"{USER}/{THREAD}/upload/график.png", self.PNG)
        key = ObjectKey.build(USER, THREAD, "график.png", "el-1")

        sink = ElementSink()
        await ImageViewer().open(key, "image/png", sink.push)

        url = sink.shown[0].url
        if "session_id" in url:
            raise AssertionError('"session_id" not in url')
        if not (urlsplit(url).path.endswith(quote(key.name, safe=""))):
            raise AssertionError('url.endswith(quote(key.name, safe=""))')
        if list(cl.context.session.files.values()):
            raise AssertionError("not list(cl.context.session.files.values())")

    @pytest.mark.anyio
    async def test_viewers_cover_expected_mimes(self) -> None:
        if not PdfViewer().handles("application/pdf"):
            raise AssertionError("PdfViewer handles application/pdf")
        if not MarkdownViewer().handles("text/markdown"):
            raise AssertionError("MarkdownViewer handles text/markdown")
        if not LogViewer().handles("text/plain"):
            raise AssertionError("LogViewer handles text/plain")
        if not VideoViewer().handles("video/mp4"):
            raise AssertionError("VideoViewer handles video/mp4")
        if not AudioViewer().handles("audio/mpeg"):
            raise AssertionError("AudioViewer handles audio/mpeg")

    @pytest.mark.anyio
    async def test_click_opens_png_end_to_end(
        self, storage: LocalStorageClient, http_context: None
    ) -> None:
        """Сценарий из бага: bash сгенерировал png — панель обязана показать."""
        CanvasViewers.register_all()
        await put_bytes(storage, f"{USER}/{THREAD}/upload/график.png", self.PNG)

        opened = await CanvasOpener(ThreadSurfaces()).show(
            f"/workspace/{THREAD}/upload/график.png",
            "image/png",
            CanvasScope(user_id=USER, thread_id=THREAD, workspace=MOUNT),
        )

        if opened.label != "график.png":
            raise AssertionError(opened.label)


class TestStorageWindows:
    """Окна текстового файла storage: чтение по смещению, стыки по строкам."""

    LINES = 5000
    """~110 КБ: больше окна журнала, файл целиком в окно не помещается."""

    @staticmethod
    def _body(lines: int) -> bytes:
        rendered: list[str] = []
        for index in range(lines):
            rendered.append(f"line-{index:06d} payload")
        return ("\n".join(rendered) + "\n").encode()

    async def _windows(
        self, storage: LocalStorageClient, body: bytes
    ) -> rendering_canvas.StorageWindows:
        object_key = f"{USER}/{THREAD}/upload/run.log"
        await put_bytes(storage, object_key, body)
        return rendering_canvas.StorageWindows(storage, object_key)

    @pytest.mark.anyio
    async def test_first_window_is_line_aligned(
        self, storage: LocalStorageClient
    ) -> None:
        body = self._body(self.LINES)
        windows = await self._windows(storage, body)

        piece = await windows.slice_at(0)

        if piece.offset != 0:
            raise AssertionError("piece.offset == 0")
        if piece.size != len(body):
            raise AssertionError("piece.size == len(body)")
        if len(piece.text.encode()) > piece.window:
            raise AssertionError("len(piece.text.encode()) <= piece.window")
        if not piece.text.endswith("\n"):
            raise AssertionError('piece.text.endswith("\\n")')

    @pytest.mark.anyio
    async def test_forward_chain_rebuilds_the_file(
        self, storage: LocalStorageClient
    ) -> None:
        """Цепочка окон встык собирает файл байт в байт — без чтения целиком."""
        body = self._body(self.LINES)
        windows = await self._windows(storage, body)

        collected = bytearray()
        offset = 0
        while offset < len(body):
            piece = await windows.slice_at(offset)
            if piece.offset != offset:
                raise AssertionError("piece.offset == offset")
            collected.extend(piece.text.encode())
            offset = piece.end

        if bytes(collected) != body:
            raise AssertionError("bytes(collected) == body")

    @pytest.mark.anyio
    async def test_backward_window_joins_at_the_edge(
        self, storage: LocalStorageClient
    ) -> None:
        body = self._body(self.LINES)
        windows = await self._windows(storage, body)

        first = await windows.slice_at(0)
        back = await windows.slice_before(first.end)

        if back.end != first.end:
            raise AssertionError("back.end == first.end")

    @pytest.mark.anyio
    async def test_negative_offset_gives_the_tail(
        self, storage: LocalStorageClient
    ) -> None:
        body = self._body(self.LINES)
        windows = await self._windows(storage, body)

        tail = await windows.slice_at(-1)

        if tail.end != len(body):
            raise AssertionError("tail.end == len(body)")
        if not tail.text.endswith(f"line-{self.LINES - 1:06d} payload\n"):
            raise AssertionError("tail.text ends with the last line")


class TestLogViewer:
    """Логи workspace: первое окно в панель, файл целиком в память не едет."""

    @pytest.fixture(autouse=True)
    def panel_storage(
        self, storage: LocalStorageClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            rendering_canvas.PanelStorage, "client", staticmethod(lambda: storage)
        )

    @pytest.mark.anyio
    async def test_content_is_the_first_window(
        self, storage: LocalStorageClient
    ) -> None:
        body = TestStorageWindows._body(TestStorageWindows.LINES)
        await put_bytes(storage, f"{USER}/{THREAD}/upload/run.log", body)
        key = ObjectKey.build(USER, THREAD, "run.log", "el-1")

        content = await rendering_canvas.LogViewer().content(key, ContentMime.PLAIN)

        if content.kind is not CanvasKind.STREAM:
            raise AssertionError("content.kind is CanvasKind.STREAM")
        if content.stream is None:
            raise AssertionError("content.stream is not None")
        if content.stream.offset != 0:
            raise AssertionError("content.stream.offset == 0")
        if content.stream.size != len(body):
            raise AssertionError("content.stream.size == len(body)")
        if len(content.text.encode()) > content.stream.window:
            raise AssertionError("len(content.text.encode()) <= content.stream.window")
        if not content.nonce:
            raise AssertionError("content.nonce")
        if f"/canvas/{THREAD}/upload/" not in content.url:
            raise AssertionError('f"/canvas/{THREAD}/upload/" in content.url')

    @pytest.mark.anyio
    async def test_missing_file_is_refused(self, storage: LocalStorageClient) -> None:
        key = ObjectKey.build(USER, THREAD, "absent.log", "el-1")

        with pytest.raises(CanvasError) as failure:
            await rendering_canvas.LogViewer().content(key, ContentMime.PLAIN)

        if failure.value.kind != CanvasErrorKind.FILE_NOT_FOUND:
            raise AssertionError("failure.value.kind == CanvasErrorKind.FILE_NOT_FOUND")

    @pytest.mark.anyio
    async def test_watch_source_sees_appends(self, storage: LocalStorageClient) -> None:
        object_key = f"{USER}/{THREAD}/upload/run.log"
        await put_bytes(storage, object_key, b"first\n")
        key = ObjectKey.build(USER, THREAD, "run.log", "el-1")

        source = rendering_canvas.LogViewer().watch_source(key)
        if source is None:
            raise AssertionError("source is not None")

        first = await source.probe()

        await put_bytes(storage, object_key, b"first\nsecond\n")
        second = await source.probe()

        if first is None or second is None:
            raise AssertionError("first is not None and second is not None")
        if second.revision == first.revision:
            raise AssertionError("second.revision != first.revision")
        if second.size <= first.size:
            raise AssertionError("second.size > first.size")


class TestFileWindowAction:
    """Действие окна для файла workspace: тот же контракт, что у журнала."""

    @pytest.fixture(autouse=True)
    def panel_storage(
        self, storage: LocalStorageClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            rendering_canvas.PanelStorage, "client", staticmethod(lambda: storage)
        )

    @pytest.mark.anyio
    async def test_windows_walk_the_file(self, storage: LocalStorageClient) -> None:
        body = TestStorageWindows._body(TestStorageWindows.LINES)
        await put_bytes(storage, f"{USER}/{THREAD}/upload/run.log", body)
        path = f"/workspace/{THREAD}/upload/run.log"

        first = await rendering_canvas.StreamActions.window(
            USER, THREAD, {"path": path, "offset": 0}, NO_REMOTE
        )
        follow_up = await rendering_canvas.StreamActions.window(
            USER, THREAD, {"path": path, "offset": first["stream"]["end"]}, NO_REMOTE
        )

        if first["stream"]["offset"] != 0:
            raise AssertionError('first["stream"]["offset"] == 0')
        if follow_up["stream"]["offset"] != first["stream"]["end"]:
            raise AssertionError('follow_up["stream"]["offset"] == first["stream"]["e…')
        if first["stream"]["size"] != len(body):
            raise AssertionError('first["stream"]["size"] == len(body)')

    @pytest.mark.anyio
    async def test_missing_file_gives_empty_answer(
        self, storage: LocalStorageClient
    ) -> None:
        answer = await rendering_canvas.StreamActions.window(
            USER,
            THREAD,
            {"path": f"/workspace/{THREAD}/upload/absent.log", "offset": 0},
            NO_REMOTE,
        )

        if answer != {}:
            raise AssertionError("answer == {}")


class TestActionsWithoutCallContext:
    """Действия фронта идут из сессии чата: контекста вызова у клика нет."""

    @pytest.mark.anyio
    async def test_content_action_needs_no_call_context(
        self, call_stand: CallStand
    ) -> None:
        call_stand.clear()
        action = cl.Action(
            name=CanvasAction.CONTENT.value,
            payload=CanvasFileRequest(
                path=f"/workspace/{THREAD}/upload/a.log", mime=ContentMime.PLAIN.value
            ).model_dump(),
        )

        request = CanvasFileRequest.model_validate(action.payload)
        content = await CanvasOpener(ThreadSurfaces()).content(
            request.path,
            request.mime,
            watch=True,
            scope=CanvasScope(user_id=USER, thread_id=THREAD, workspace=MOUNT),
        )
        described = content.props()

        if described.get("path") != f"/workspace/{THREAD}/upload/a.log":
            raise AssertionError(f"описание файла из сессии: {described}")
