"""Канвас: реестр вьюверов, список файлов и отказы — общий слой без mermaid."""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar, cast
from urllib.parse import quote

import chainlit as cl
import pytest

from boba.chainlit.agent.tools import canvas as canvas_module
from boba.chainlit.agent.tools.canvas import (
    AudioViewer,
    CanvasOpener,
    CanvasToolConfig,
    ImageViewer,
    PdfViewer,
    TextViewer,
    VideoViewer,
    build_canvas_tools,
)
from boba.chainlit.data.storage import LocalStorageClient
from boba.chainlit.domain import session as session_module
from boba.chainlit.domain.keys import ObjectKey
from boba.chainlit.domain.session import SessionKind
from boba.chainlit.infra.config import LocalStorageConfig
from boba.chainlit.rendering.canvas import (
    CanvasContent,
    CanvasError,
    CanvasErrorKind,
    CanvasKind,
    CanvasPanel,
    CanvasPush,
    CanvasRegistry,
    CanvasViewer,
    OpenedCanvas,
)
from boba.toolkit.binaries import TrustedBinaries
from boba.toolkit.result import CustomElementResult, ErrorResult
from boba.workspace.launcher import LauncherConfig

THREAD = "11111111-1111-1111-1111-111111111111"
USER = "7"


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    """Заглушка сессионной фикстуры conftest: БД этим тестам не нужна."""


class FakeViewer:
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

        return OpenedCanvas(label=key.name, path=key.in_workspace(), link=link)


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
        if not (isinstance(CanvasRegistry.viewer_for("notes.md"), TextViewer)):
            raise AssertionError('isinstance(CanvasRegistry.viewer_for("notes.md"), T…')
        if not (isinstance(CanvasRegistry.viewer_for("demo.mp4"), VideoViewer)):
            raise AssertionError('isinstance(CanvasRegistry.viewer_for("demo.mp4"), V…')
        if not (isinstance(CanvasRegistry.viewer_for("voice.mp3"), AudioViewer)):
            raise AssertionError('isinstance(CanvasRegistry.viewer_for("voice.mp3"), …')
        if CanvasRegistry.viewer_for("data.bin") is not None:
            raise AssertionError('CanvasRegistry.viewer_for("data.bin") is None')

    def test_schema_fields(self) -> None:
        tool = build_canvas_tools(CanvasToolConfig())[0]
        schema = cast(type[Any], tool.tool_call_schema)
        if set(schema.model_fields) != {"path"}:
            raise AssertionError('set(schema.model_fields) == {"path"}')


class TestRefusal:
    """Отказ доезжает до LLM ошибкой с причиной, а не исключением."""

    @pytest.mark.anyio
    async def test_without_session(self) -> None:
        _, result = await CanvasOpener().open(f"/workspace/{THREAD}/mermaid/a.mmd")

        if not (isinstance(result, ErrorResult)):
            raise AssertionError("isinstance(result, ErrorResult)")
        if result.error_kind != SessionKind.NO_SESSION:
            raise AssertionError("result.error_kind == SessionKind.NO_SESSION")

    @pytest.mark.anyio
    async def test_path_outside_thread(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(session_module, "current_user_id", lambda: USER)
        monkeypatch.setattr(session_module, "current_thread_id", lambda: THREAD)

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
        launcher=LauncherConfig(
            mount_wait_sec=1.0,
            mount_poll_sec=0.1,
            shutdown_wait_sec=1.0,
            lock_wait_sec=1.0,
            copy_chunk_bytes=65536,
        ),
        binaries=TrustedBinaries(dirs=("/usr/bin", "/bin")),
    )
    client = LocalStorageClient(config)
    layer = _StorageOnlyLayer(client)

    monkeypatch.setattr(session_module, "current_user_id", lambda: USER)
    monkeypatch.setattr(session_module, "current_thread_id", lambda: THREAD)
    monkeypatch.setattr(CanvasOpener, "_storage", staticmethod(lambda: client))
    monkeypatch.setattr(canvas_module, "get_data_layer", lambda: layer)

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

        opened = await CanvasOpener().show(f"/workspace/{THREAD}/mermaid/a.mmd")

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

        from boba.chainlit.rendering import canvas as rendering_canvas

        monkeypatch.setattr(rendering_canvas.cl.CustomElement, "send", capture)
        monkeypatch.setattr(rendering_canvas.cl, "ElementSidebar", Sidebar)

        await CanvasOpener().show(f"/workspace/{THREAD}/upload/a.png")
        await CanvasOpener().show(f"/workspace/{THREAD}/upload/b.png")

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
        if ".md" not in TextViewer.suffixes:
            raise AssertionError('".md" in TextViewer.suffixes')
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
