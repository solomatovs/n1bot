"""Канвас: реестр вьюверов, список файлов и отказы — общий слой без mermaid."""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar, cast
from urllib.parse import quote
from uuid import UUID

import chainlit as cl
import pytest
from chainlit_stand import use_session
from pydantic import BaseModel

from boba.canvas.canvas import (
    CanvasAction,
    CanvasContent,
    CanvasError,
    CanvasErrorKind,
    CanvasKind,
    CanvasPush,
    CanvasRegistry,
    CanvasViewer,
    OpenedCanvas,
)
from boba.canvas.keys import ObjectKey
from boba.chainlit.canvas import panel as rendering_canvas
from boba.chainlit.canvas.panel import CanvasPanel
from boba.chainlit.canvas.tools import (
    AudioViewer,
    CanvasActions,
    CanvasOpener,
    CanvasScope,
    CanvasToolConfig,
    ImageViewer,
    LogViewer,
    MarkdownViewer,
    PdfViewer,
    VideoViewer,
    build_canvas_tools,
)
from boba.chainlit.data.storage import LocalStorageClient
from boba.chainlit.infra.config import LocalStorageConfig
from boba.identity.context import CallContext, ContextKind
from boba.toolkit.result import CustomElementResult, ErrorResult
from boba.workspace.binaries import TrustedBinaries
from boba.workspace.launcher import MountingConfig

THREAD = "11111111-1111-1111-1111-111111111111"
USER = str(UUID(int=7))


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    """Заглушка сессионной фикстуры conftest: БД этим тестам не нужна."""


class FakeViewer(CanvasViewer):
    """Вьювер под тест: берётся за своё расширение и запоминает показанное."""

    suffixes: ClassVar[frozenset[str]] = frozenset({".mmd"})

    def __init__(self, suffix: str) -> None:
        self._suffix = suffix
        self.opened: list[ObjectKey] = []

    def handles(self, name: str) -> bool:
        return name.endswith(self._suffix)

    async def content(self, key: ObjectKey) -> CanvasContent:
        return CanvasContent(
            kind=CanvasKind.MERMAID,
            path=key.in_workspace(),
            label=key.name,
            text="erDiagram",
        )

    async def open(self, key: ObjectKey, push: CanvasPush) -> OpenedCanvas:
        self.opened.append(key)
        await push(await self.content(key))

        link = CustomElementResult(
            element="CanvasLink",
            props={"path": key.in_workspace(), "label": key.name},
            title=key.name,
        )

        return OpenedCanvas(
            label=key.name, path=key.in_workspace(), nonce="n-1", link=link
        )

    def watch_source(self, key: ObjectKey) -> None:
        return None


class OtherViewer(FakeViewer):
    """Второй тип вьювера: реестр различает вьюверы по классу."""


@pytest.fixture(autouse=True)
def empty_registry() -> None:
    CanvasRegistry.reset()


@pytest.fixture
async def http_context() -> None:
    """ElementSidebar пишет в emitter сессии — без контекста панели не открыться."""
    from chainlit.context import init_http_context

    init_http_context()


class TestRegistry:
    def test_viewer_is_found_by_suffix(self) -> None:
        viewer: CanvasViewer = FakeViewer(".mmd")
        CanvasRegistry.register(viewer)

        if CanvasRegistry.viewer_for("a.mmd") is not viewer:
            raise AssertionError('CanvasRegistry.viewer_for("a.mmd") is viewer')
        if CanvasRegistry.viewer_for("a.pdf") is not None:
            raise AssertionError('CanvasRegistry.viewer_for("a.pdf") is None')

    def test_registration_is_idempotent_per_type(self) -> None:
        """Тулы собираются на каждую сессию — реестр не должен расти."""
        first = FakeViewer(".mmd")
        second = FakeViewer(".mmd")

        CanvasRegistry.register(first)
        CanvasRegistry.register(second)

        if CanvasRegistry.viewer_for("a.mmd") is not second:
            raise AssertionError('CanvasRegistry.viewer_for("a.mmd") is second')
        if CanvasRegistry.viewers_hint() != "FakeViewer":
            raise AssertionError('CanvasRegistry.viewers_hint() == "FakeViewer"')

    def test_different_types_live_together(self) -> None:
        CanvasRegistry.register(FakeViewer(".mmd"))
        CanvasRegistry.register(OtherViewer(".pdf"))

        if CanvasRegistry.viewers_hint() != "FakeViewer, OtherViewer":
            raise AssertionError('CanvasRegistry.viewers_hint() == "FakeViewer, Other…')

    def test_hint_without_viewers(self) -> None:
        if CanvasRegistry.viewers_hint() != "none registered":
            raise AssertionError('CanvasRegistry.viewers_hint() == "none registered"')


class TestPanel:
    @pytest.mark.anyio
    async def test_open_without_viewer_names_the_file(self, http_context: None) -> None:
        key = ObjectKey.build(USER, THREAD, "notes.txt", "el-1")

        with pytest.raises(CanvasError) as failure:
            await CanvasPanel.open(key)

        if failure.value.kind != CanvasErrorKind.NO_VIEWER:
            raise AssertionError("failure.value.kind == CanvasErrorKind.NO_VIEWER")
        if "notes.txt" not in str(failure.value):
            raise AssertionError('"notes.txt" in str(failure.value)')


class TestToolInterface:
    def test_tool_name(self) -> None:
        tools = build_canvas_tools(CanvasToolConfig())
        if [t.name for t in tools] != ["canvas_open"]:
            raise AssertionError('[t.name for t in tools] == ["canvas_open"]')

    def test_build_registers_file_viewers(self) -> None:
        """PNG от bash/python-тулов обязан показываться — ход из бага."""
        build_canvas_tools(CanvasToolConfig())

        if not (isinstance(CanvasRegistry.viewer_for("график.png"), ImageViewer)):
            raise AssertionError('isinstance(CanvasRegistry.viewer_for("график.png"),…')
        if not (isinstance(CanvasRegistry.viewer_for("report.PDF"), PdfViewer)):
            raise AssertionError('isinstance(CanvasRegistry.viewer_for("report.PDF"),…')
        if not (isinstance(CanvasRegistry.viewer_for("notes.md"), MarkdownViewer)):
            raise AssertionError('isinstance(CanvasRegistry.viewer_for("notes.md"), M…')
        if not (isinstance(CanvasRegistry.viewer_for("run.log"), LogViewer)):
            raise AssertionError('isinstance(CanvasRegistry.viewer_for("run.log"), Lo…')
        if not (isinstance(CanvasRegistry.viewer_for("demo.mp4"), VideoViewer)):
            raise AssertionError('isinstance(CanvasRegistry.viewer_for("demo.mp4"), V…')
        if not (isinstance(CanvasRegistry.viewer_for("voice.mp3"), AudioViewer)):
            raise AssertionError('isinstance(CanvasRegistry.viewer_for("voice.mp3"), …')
        if CanvasRegistry.viewer_for("data.bin") is not None:
            raise AssertionError('CanvasRegistry.viewer_for("data.bin") is None')

    def test_schema_fields(self) -> None:
        tool = build_canvas_tools(CanvasToolConfig())[0]
        schema = cast(type[BaseModel], tool.tool_call_schema)
        if set(schema.model_fields) != {"path"}:
            raise AssertionError('set(schema.model_fields) == {"path"}')


class TestRefusal:
    """Отказ доезжает до LLM ошибкой с причиной, а не исключением."""

    @pytest.mark.anyio
    async def test_without_session(self) -> None:
        opener = CanvasOpener()

        _, result = await opener.open(f"/workspace/{THREAD}/mermaid/a.mmd")

        if not (isinstance(result, ErrorResult)):
            raise AssertionError("isinstance(result, ErrorResult)")
        if result.error_kind != ContextKind.NO_CONTEXT:
            raise AssertionError("result.error_kind == ContextKind.NO_CONTEXT")

    @pytest.mark.anyio
    async def test_path_outside_thread(self, monkeypatch: pytest.MonkeyPatch) -> None:
        use_session(monkeypatch, user_id=USER, thread_id=THREAD)

        _, result = await CanvasOpener().open("/etc/passwd")

        if not (isinstance(result, ErrorResult)):
            raise AssertionError("isinstance(result, ErrorResult)")
        if result.error_kind != CanvasErrorKind.BAD_PATH:
            raise AssertionError("result.error_kind == CanvasErrorKind.BAD_PATH")


class _StorageOnlyLayer:
    """Доступ к слою в тесте: канвасу нужен только storage."""

    def __init__(self, storage: LocalStorageClient) -> None:
        self.storage = storage


@pytest.fixture
def storage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> LocalStorageClient:
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
    )
    client = LocalStorageClient(config)
    layer = _StorageOnlyLayer(client)

    use_session(monkeypatch, user_id=USER, thread_id=THREAD)
    monkeypatch.setattr(rendering_canvas, "get_data_layer", lambda: layer)

    return client


class TestShow:
    """Панель показывает один файл: тот же код у тула и клика по ссылке."""

    @pytest.mark.anyio
    async def test_show_opens_the_file(
        self, storage: LocalStorageClient, http_context: None
    ) -> None:
        viewer = FakeViewer(".mmd")
        CanvasRegistry.register(viewer)
        await storage.upload_file(f"{USER}/{THREAD}/mermaid/a.mmd", "erDiagram")

        opened = await CanvasOpener().show(
            f"/workspace/{THREAD}/mermaid/a.mmd", CanvasScope.of_context()
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
        await storage.upload_file(f"{USER}/{THREAD}/upload/a.png", png)
        await storage.upload_file(f"{USER}/{THREAD}/upload/b.png", png)

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

        await CanvasOpener().show(
            f"/workspace/{THREAD}/upload/a.png", CanvasScope.of_context()
        )
        await CanvasOpener().show(
            f"/workspace/{THREAD}/upload/b.png", CanvasScope.of_context()
        )

        if [e.props["label"] for e in shown] != ["a.png", "b.png"]:
            raise AssertionError('[e.props["label"] for e in shown] == ["a.png", "b.p…')
        if [e.name for e in shown] != [CanvasPanel.VIEW_ELEMENT] * 2:
            raise AssertionError("[e.name for e in shown] == [CanvasPanel.VIEW_ELEMEN…")
        if [e.display for e in shown] != ["side", "side"]:
            raise AssertionError('[e.display for e in shown] == ["side", "side"]')
        if shown[0].id != CanvasPanel.CONTENT_ID:
            raise AssertionError("shown[0].id == CanvasPanel.CONTENT_ID")
        if shown[1].id != CanvasPanel.CONTENT_ID:
            raise AssertionError("shown[1].id == CanvasPanel.CONTENT_ID")
        if titles != [CanvasPanel.TITLE, CanvasPanel.TITLE]:
            raise AssertionError("titles == [CanvasPanel.TITLE, CanvasPanel.TITLE]")


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
        await storage.upload_file(f"{USER}/{THREAD}/upload/график.png", self.PNG)
        key = ObjectKey.build(USER, THREAD, "график.png", "el-1")

        sink = ElementSink()
        opened = await ImageViewer().open(key, sink.push)

        if opened.label != "график.png":
            raise AssertionError('opened.label == "график.png"')
        if not (isinstance(opened.link, CustomElementResult)):
            raise AssertionError("isinstance(opened.link, CustomElementResult)")
        if opened.link.props["path"] != opened.path:
            raise AssertionError('opened.link.props["path"] == opened.path')
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
        await storage.upload_file(f"{USER}/{THREAD}/upload/график.png", self.PNG)
        key = ObjectKey.build(USER, THREAD, "график.png", "el-1")

        sink = ElementSink()
        await ImageViewer().open(key, sink.push)

        url = sink.shown[0].url
        if "session_id" in url:
            raise AssertionError('"session_id" not in url')
        if not (url.endswith(quote(key.name, safe=""))):
            raise AssertionError('url.endswith(quote(key.name, safe=""))')
        if list(cl.context.session.files.values()):
            raise AssertionError("not list(cl.context.session.files.values())")

    @pytest.mark.anyio
    async def test_viewers_cover_expected_suffixes(self) -> None:
        if ".pdf" not in PdfViewer.suffixes:
            raise AssertionError('".pdf" in PdfViewer.suffixes')
        if ".md" not in MarkdownViewer.suffixes:
            raise AssertionError('".md" in MarkdownViewer.suffixes')
        if ".log" not in LogViewer.suffixes:
            raise AssertionError('".log" in LogViewer.suffixes')
        if ".mp4" not in VideoViewer.suffixes:
            raise AssertionError('".mp4" in VideoViewer.suffixes')
        if ".mp3" not in AudioViewer.suffixes:
            raise AssertionError('".mp3" in AudioViewer.suffixes')

    @pytest.mark.anyio
    async def test_tool_opens_png_end_to_end(
        self, storage: LocalStorageClient, http_context: None
    ) -> None:
        """Сценарий из бага: bash сгенерировал png — canvas_open обязан показать."""
        build_canvas_tools(CanvasToolConfig())
        await storage.upload_file(f"{USER}/{THREAD}/upload/график.png", self.PNG)

        content, result = await CanvasOpener().open(
            f"/workspace/{THREAD}/upload/график.png"
        )

        if isinstance(result, ErrorResult):
            raise AssertionError("not isinstance(result, ErrorResult)")
        if "график.png" not in content:
            raise AssertionError('"график.png" in content')


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
        await storage.upload_file(object_key, body)
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
        await storage.upload_file(f"{USER}/{THREAD}/upload/run.log", body)
        key = ObjectKey.build(USER, THREAD, "run.log", "el-1")

        content = await rendering_canvas.LogViewer().content(key)

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
            await rendering_canvas.LogViewer().content(key)

        if failure.value.kind != CanvasErrorKind.FILE_NOT_FOUND:
            raise AssertionError("failure.value.kind == CanvasErrorKind.FILE_NOT_FOUND")

    @pytest.mark.anyio
    async def test_watch_source_sees_appends(self, storage: LocalStorageClient) -> None:
        object_key = f"{USER}/{THREAD}/upload/run.log"
        await storage.upload_file(object_key, b"first\n")
        key = ObjectKey.build(USER, THREAD, "run.log", "el-1")

        source = rendering_canvas.LogViewer().watch_source(key)
        if source is None:
            raise AssertionError("source is not None")

        first = await source.probe()

        await storage.upload_file(object_key, b"first\nsecond\n", overwrite=True)
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
        await storage.upload_file(f"{USER}/{THREAD}/upload/run.log", body)
        path = f"/workspace/{THREAD}/upload/run.log"

        first = await rendering_canvas.StreamActions.window(
            USER, THREAD, {"path": path, "offset": 0}
        )
        follow_up = await rendering_canvas.StreamActions.window(
            USER, THREAD, {"path": path, "offset": first["stream"]["end"]}
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
        )

        if answer != {}:
            raise AssertionError("answer == {}")


class TestActionsWithoutCallContext:
    """Действия фронта идут из сессии чата: контекста вызова у клика нет."""

    @pytest.mark.anyio
    async def test_content_action_needs_no_call_context(self) -> None:
        CallContext.reset()
        action = cl.Action(
            name=CanvasAction.CONTENT.value,
            payload={CanvasAction.PATH.value: f"/workspace/{THREAD}/upload/a.log"},
        )

        described = await CanvasActions.content(
            action, CanvasScope(user_id=USER, thread_id=THREAD)
        )

        if described.get("path") != f"/workspace/{THREAD}/upload/a.log":
            raise AssertionError(f"описание файла из сессии: {described}")
