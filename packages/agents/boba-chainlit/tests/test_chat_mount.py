"""ChatMount целиком: тела плагина canvas через обвязку чата.

Тело пишет и читает файлы workspace, результат несёт items, обвязка
монтирует их на поверхность: вложение — строкой элемента и показом через
порт хода, панель — содержимым вьювера плюс ссылкой в переписке, вердикт
браузера по диаграмме — ErrorResult для модели.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import pytest
from chainlit_stand import ChatSessionStand, FakeTurn
from langchain_core.messages import ToolCall

from boba.canvas.canvas import CanvasErrorKind, RenderVerdicts
from boba.canvas.keys import WorkspaceMount
from boba.chainlit.canvas.panel import CanvasPanel
from boba.chainlit.canvas.tools import CanvasViewers
from boba.chainlit.data.data_layer import AttachmentDataLayer
from boba.chainlit.data.storage import LocalStorageClient
from boba.chainlit.domain.keys import AttachmentLinks
from boba.chainlit.infra.config import LocalStorageConfig
from boba.chainlit.rendering.mount import ChatAttachments, ChatMount
from boba.stand.refs import StandRefs
from boba.stand_core.context import CallStand
from boba.tool.canvas.tools import TOOLS, CanvasToolConfig
from boba.toolkit.result import CanvasResult, ErrorResult, FileResult
from boba.toolrun.bridge import ToolBridge
from boba.toolrun.call_id import CallFields
from boba.toolrun.callvalues import CallContextValues
from boba.toolrun.injected import InjectedConfig, StaticConfig
from boba.toolrun.run_log import ToolRunLogger
from boba.workspace.binaries import TrustedBinaries
from boba.workspace.launcher import MountingConfig

THREAD = "11111111-1111-1111-1111-111111111111"
USER = str(UUID(int=7))
ER_SPEC = "erDiagram\n    CUSTOMER ||--o{ ORDER : has"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    """Заглушка сессионной фикстуры conftest: БД этим тестам не нужна."""


@pytest.fixture
async def http_context() -> None:
    """cl.CustomElement требует контекст chainlit и живой цикл событий."""
    from chainlit.context import init_http_context

    init_http_context()


class _StorageOnlyLayer:
    """Слой данных под тест: storage, ссылки и элементы, ушедшие в ленту."""

    def __init__(self, storage: LocalStorageClient) -> None:
        self.storage = storage
        self.links = AttachmentLinks(prefix="/boba")
        self.elements: list[Any] = []

    async def create_element(self, element: Any) -> None:
        self.elements.append(element)


class Stand:
    """Инструменты canvas в процессе теста с обвязками загрузчика чата.

    Workspace пользователя — каталог storage: тело пишет туда же, откуда
    читает вьювер панели.
    """

    def __init__(
        self,
        chat_session: ChatSessionStand,
        runtime_stand: StandRefs,
        call_stand: CallStand,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
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
        self._calls = call_stand
        self._runtime = runtime_stand
        self.storage = LocalStorageClient(config)
        self.layer = _StorageOnlyLayer(self.storage)
        self.turn = FakeTurn()
        self.pushed: list[Any] = []

        # запуск открывается контекстом сессии: телу и обвязке нужен контекст
        # чата с поверхностью, реестру — запись о порте хода
        chat_session.use(user_id=USER, thread_id=THREAD)
        self.run = runtime_stand.runs.open(
            call_stand.contexts.current(), cast(Any, self.turn)
        )
        self.run.__enter__()
        monkeypatch.setattr(
            AttachmentDataLayer, "require", classmethod(lambda cls: self.layer)
        )
        WorkspaceMount.configure(str(tmp_path / USER))

        async def capture(cls: Any, content: Any) -> None:
            self.pushed.append(content)

        monkeypatch.setattr(CanvasPanel, "_push", classmethod(capture))
        CanvasViewers.register_all()

        self.tools = {tool.name: tool for tool in self._bridged()}

    def _bridged(self) -> list[Any]:
        bridged = [ToolBridge.as_structured_tool(tool) for tool in TOOLS]
        CallContextValues(self._calls.contexts).bind_all(bridged)
        InjectedConfig(
            lambda name, annotation: CanvasToolConfig(max_chars=32000), StaticConfig()
        ).bind_all(bridged)
        ChatMount(self._calls.contexts, self._runtime.runs).guard_all(bridged)
        CallFields().attach_all(bridged)
        ToolRunLogger(
            self._runtime.journals, self._calls.contexts, self._runtime.ambient
        ).guard_all(bridged)
        return bridged

    async def call(self, name: str, args: dict[str, Any]) -> Any:
        request = {"name": name, "args": args, "id": "call-1", "type": "tool_call"}
        message = await self.tools[name].ainvoke(request)

        return message.artifact

    async def call_with_verdict(
        self, name: str, args: dict[str, Any], verdict: dict[str, Any]
    ) -> Any:
        """Вызов, который ждёт вердикт браузера: он приходит после показа."""
        request = {"name": name, "args": args, "id": "call-1", "type": "tool_call"}
        call = asyncio.ensure_future(self.tools[name].ainvoke(request))

        await asyncio.wait_for(self._await_push(), 5)
        RenderVerdicts.report({"nonce": self.pushed[0].nonce, **verdict})
        message = await asyncio.wait_for(call, 5)

        return message.artifact

    async def _await_push(self) -> None:
        while not self.pushed:
            await asyncio.sleep(0.01)

    def upload_path(self, name: str) -> str:
        return f"{WorkspaceMount.path()}/{THREAD}/upload/{name}"


@pytest.fixture
def stand(
    chat_session: ChatSessionStand,
    runtime_stand: StandRefs,
    call_stand: CallStand,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Any:
    built = Stand(chat_session, runtime_stand, call_stand, tmp_path, monkeypatch)
    yield built
    built.run.__exit__(None, None, None)


class TestSendFile:
    @pytest.mark.anyio
    async def test_attachment_reaches_the_feed(
        self, stand: Stand, http_context: None
    ) -> None:
        await stand.storage.upload_file(f"{USER}/{THREAD}/upload/report.pdf", b"%PDF")

        result = await stand.call(
            "send_file", {"path": stand.upload_path("report.pdf")}
        )

        if not isinstance(result, FileResult):
            raise AssertionError(result)

        element = stand.layer.elements[0]
        if element.name != "report.pdf" or element.mime != "application/pdf":
            raise AssertionError(element)
        if element.for_id != FakeTurn.ANSWER_STEP:
            raise AssertionError(element.for_id)
        if "/report" not in element.url and element.url == "":
            raise AssertionError(element.url)

        shown_ids = [shown[0] for shown in stand.turn.shown]
        if shown_ids != ["call-1"]:
            raise AssertionError(stand.turn.shown)

    @pytest.mark.anyio
    async def test_missing_file_is_an_error_without_elements(
        self, stand: Stand, http_context: None
    ) -> None:
        result = await stand.call("send_file", {"path": stand.upload_path("no.pdf")})

        if not isinstance(result, ErrorResult):
            raise AssertionError(result)
        if stand.layer.elements != []:
            raise AssertionError(stand.layer.elements)


class TestCanvasOpen:
    @pytest.mark.anyio
    async def test_png_goes_to_the_panel_and_the_feed(
        self, stand: Stand, http_context: None
    ) -> None:
        await stand.storage.upload_file(f"{USER}/{THREAD}/upload/chart.png", PNG)

        result = await stand.call(
            "canvas_open", {"path": stand.upload_path("chart.png")}
        )

        if not isinstance(result, CanvasResult):
            raise AssertionError(result)
        if stand.pushed[0].label != "chart.png":
            raise AssertionError(stand.pushed)

        link = stand.layer.elements[0]
        if link.props["label"] != "chart.png" or link.for_id != FakeTurn.ANSWER_STEP:
            raise AssertionError(link)


class TestDiagramSave:
    @pytest.mark.anyio
    async def test_render_failure_becomes_tool_error(
        self, stand: Stand, http_context: None
    ) -> None:
        """Битую спеку ловит только браузер — модель обязана узнать об этом."""
        result = await stand.call_with_verdict(
            "diagram_save",
            {"name": "orders.mmd", "spec": ER_SPEC},
            {"ok": False, "error": "Parse error on line 5"},
        )

        if not isinstance(result, ErrorResult):
            raise AssertionError(result)
        if result.error_kind != CanvasErrorKind.RENDER_FAILED:
            raise AssertionError(result.error_kind)
        if "Parse error on line 5" not in result.message:
            raise AssertionError(result.message)
        if "diagram saved" not in result.message:
            raise AssertionError(result.message)
        if stand.layer.elements != []:
            raise AssertionError("a failed diagram leaves no card in the feed")

    @pytest.mark.anyio
    async def test_rendered_diagram_card_goes_to_the_feed(
        self, stand: Stand, http_context: None
    ) -> None:
        result = await stand.call_with_verdict(
            "diagram_save",
            {"name": "orders.mmd", "spec": ER_SPEC},
            {"ok": True, "error": ""},
        )

        if not isinstance(result, CanvasResult):
            raise AssertionError(result)
        if not result.llm_view().startswith("diagram saved: "):
            raise AssertionError(result.llm_view())

        if stand.pushed[0].kind != "mermaid" or stand.pushed[0].text != ER_SPEC:
            raise AssertionError(stand.pushed)

        card = stand.layer.elements[0]
        if card.props["kind"] != "mermaid" or card.props["preview"] is not True:
            raise AssertionError(card.props)
        if card.for_id != FakeTurn.ANSWER_STEP:
            raise AssertionError(card.for_id)

        stored = Path(WorkspaceMount.path()) / THREAD / "mermaid" / "orders.mmd"
        if stored.read_text(encoding="utf-8") != ER_SPEC:
            raise AssertionError(stored)


class TestMcpFileBlock:
    """Картинка из результата MCP-сервера: файл в workspace треда и вложение
    у шага вызова."""

    PNG: bytes = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16

    @pytest.mark.anyio
    async def test_block_is_saved_to_the_workspace_and_attached(
        self,
        stand: Stand,
        runtime_stand: StandRefs,
        call_stand: CallStand,
        tmp_path: Path,
        http_context: None,
    ) -> None:
        files = ChatAttachments(
            call_stand.contexts,
            stand.storage,
            ChatMount(call_stand.contexts, runtime_stand.runs),
        )
        call = ToolCall(name="std_picture", args={}, id="call-9", type="tool_call")

        note = await files.attached(call, 0, "image/png", self.PNG)

        saved = list((tmp_path / USER / THREAD / "upload").glob("std_picture-*.png"))
        if len(saved) != 1 or saved[0].read_bytes() != self.PNG:
            raise AssertionError(f"the block is a file of the thread: {saved}")
        if stand.upload_path(saved[0].name) not in note:
            raise AssertionError(f"the model is told where the file is: {note}")

        element = stand.layer.elements[0]
        if element.mime != "image/png" or element.name != saved[0].name:
            raise AssertionError(f"the file is attached to the chat: {element}")

        shown = [shown[0] for shown in stand.turn.shown]
        if shown != ["call-9"]:
            raise AssertionError(f"the attachment belongs to its call: {shown}")

    @pytest.mark.anyio
    async def test_second_block_is_saved_without_a_second_attachment(
        self,
        stand: Stand,
        runtime_stand: StandRefs,
        call_stand: CallStand,
        tmp_path: Path,
        http_context: None,
    ) -> None:
        files = ChatAttachments(
            call_stand.contexts,
            stand.storage,
            ChatMount(call_stand.contexts, runtime_stand.runs),
        )
        call = ToolCall(name="std_picture", args={}, id="call-9", type="tool_call")

        await files.attached(call, 0, "image/png", self.PNG)
        await files.attached(call, 1, "image/png", self.PNG)

        saved = list((tmp_path / USER / THREAD / "upload").glob("std_picture-*.png"))
        if len(saved) != 2:
            raise AssertionError(f"every block is saved: {saved}")
        if len(stand.layer.elements) != 1:
            raise AssertionError(f"one attachment per call: {stand.layer.elements}")
