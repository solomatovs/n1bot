"""ChatMount целиком: инструменты canvas сервиса через порт чата.

Тело исполняет сервис boba-mcp: пишет и читает файлы workspace, результат
несёт items. Порт чата (MountedToolServer) монтирует их на поверхность:
вложение — строкой элемента и показом через порт хода, панель — содержимым
вьювера плюс ссылкой в переписке, вердикт браузера по диаграмме —
ErrorResult для модели.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

import pytest
from chainlit.user import PersistedUser
from chainlit_stand import (
    ChatSessionStand,
    FakeTurn,
    ServiceTools,
    ToolService,
    get_bytes,
    put_bytes,
)
from langchain_core.messages import ToolCall

from boba.canvas.canvas import CanvasErrorKind, RenderVerdicts
from boba.canvas.keys import WorkspaceMount
from boba.chainlit.canvas.panel import CanvasPanel
from boba.chainlit.canvas.tools import CanvasViewers
from boba.chainlit.data.data_layer import AttachmentDataLayer
from boba.chainlit.domain.keys import AttachmentLinks
from boba.chainlit.rendering.mount import (
    ChatAttachments,
    ChatMount,
    MountedToolServer,
)
from boba.identity.api import StoredUser
from boba.identity.session import Login, UserMetadataField
from boba.runtime.storage import StorageClient
from boba.stand.refs import StandRefs
from boba.stand_core.context import CallStand
from boba.toolkit.result import CanvasResult, ErrorResult, FileResult

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(bool(ToolService.missing()), reason=ToolService.missing()),
]

THREAD = "11111111-1111-1111-1111-111111111111"
USER = str(UUID(int=7))
LOGIN = "mount-user"
PROFILE = "test"
ER_SPEC = "erDiagram\n    CUSTOMER ||--o{ ORDER : has"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
WAIT_SEC = 60.0


class _StorageOnlyLayer:
    """Слой данных под тест: storage, ссылки и элементы, ушедшие в ленту."""

    def __init__(self, storage: StorageClient) -> None:
        self.storage = storage
        self.links = AttachmentLinks(prefix="/boba")
        self.elements: list[Any] = []

    async def create_element(self, element: Any) -> None:
        self.elements.append(element)


class Stand:
    """Инструменты canvas сервиса за портом чата с обвязкой ChatMount.

    Создаётся фикстурой stand. Пользователь стенда входит в чат, его файлы
    держит сервис: хранилище чата (RemoteStorageClient) пишет туда же,
    откуда читает тело инструмента и вьювер панели.
    """

    def __init__(
        self,
        chat_session: ChatSessionStand,
        runtime_stand: StandRefs,
        call_stand: CallStand,
        tools: ServiceTools,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        meta = {
            UserMetadataField.ROLES: ["dev"],
            UserMetadataField.PROFILES: [PROFILE],
        }
        created = datetime.now(UTC)
        user = PersistedUser(
            id=USER, identifier=LOGIN, createdAt=created.isoformat(), metadata=meta
        )
        owner = StoredUser(
            id=UUID(USER), identifier=Login(LOGIN), created_at=created, meta=meta
        )

        self._calls = call_stand
        self._tools = tools
        self.storage = tools.storage(owner, PROFILE)
        self.layer = _StorageOnlyLayer(self.storage)
        self.turn = FakeTurn()
        self.pushed: list[Any] = []
        self.mount = ChatMount(call_stand.contexts, runtime_stand.runs)

        # запуск открывается контекстом сессии: обвязке нужен контекст чата с
        # поверхностью, реестру — запись о порте хода
        chat_session.sign_in(user, meta, THREAD, PROFILE)
        self.run = runtime_stand.runs.open(
            call_stand.contexts.current(), cast(Any, self.turn)
        )
        self.run.__enter__()
        monkeypatch.setattr(
            AttachmentDataLayer, "require", classmethod(lambda cls: self.layer)
        )

        async def capture(cls: Any, content: Any) -> None:
            self.pushed.append(content)

        monkeypatch.setattr(CanvasPanel, "_push", classmethod(capture))
        CanvasViewers.register_all()

    async def open(self) -> None:
        """Подключается к сервису: с этого момента известен путь workspace."""
        await self._tools.port()

    async def call(self, name: str, args: dict[str, Any]) -> Any:
        message = await asyncio.wait_for(self._submitted(name, args), WAIT_SEC)

        return message.artifact

    async def call_with_verdict(
        self, name: str, args: dict[str, Any], verdict: dict[str, Any]
    ) -> Any:
        """Вызов, который ждёт вердикт браузера: он приходит после показа."""
        call = asyncio.ensure_future(self._submitted(name, args))

        await asyncio.wait_for(self._await_push(), WAIT_SEC)
        RenderVerdicts.report({"nonce": self.pushed[0].nonce, **verdict})
        message = await asyncio.wait_for(call, WAIT_SEC)

        return message.artifact

    async def _submitted(self, name: str, args: dict[str, Any]) -> Any:
        port = MountedToolServer(
            await self._tools.port(), self.mount, self._calls.contexts
        )
        call = ToolCall(name=name, args=args, id="call-1", type="tool_call")
        pending = await port.submit([call])

        return await pending[0]

    async def _await_push(self) -> None:
        while not self.pushed:
            await asyncio.sleep(0.01)

    def upload_path(self, name: str) -> str:
        return f"{WorkspaceMount.path()}/{THREAD}/upload/{name}"

    def key(self, directory: str, name: str) -> str:
        """Ключ файла треда в хранилище чата."""
        return f"{USER}/{THREAD}/{directory}/{name}"


@pytest.fixture
async def stand(
    chat_session: ChatSessionStand,
    runtime_stand: StandRefs,
    call_stand: CallStand,
    service_tools: ServiceTools,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[Stand]:
    built = Stand(chat_session, runtime_stand, call_stand, service_tools, monkeypatch)
    try:
        await built.open()
        yield built
    finally:
        built.run.__exit__(None, None, None)


class TestSendFile:
    @pytest.mark.anyio
    async def test_attachment_reaches_the_feed(self, stand: Stand) -> None:
        await put_bytes(stand.storage, stand.key("upload", "report.pdf"), b"%PDF")

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
        self, stand: Stand
    ) -> None:
        result = await stand.call("send_file", {"path": stand.upload_path("no.pdf")})

        if not isinstance(result, ErrorResult):
            raise AssertionError(result)
        if stand.layer.elements != []:
            raise AssertionError(stand.layer.elements)


class TestCanvasOpen:
    @pytest.mark.anyio
    async def test_png_goes_to_the_panel_and_the_feed(self, stand: Stand) -> None:
        await put_bytes(stand.storage, stand.key("upload", "chart.png"), PNG)

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
    async def test_render_failure_becomes_tool_error(self, stand: Stand) -> None:
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
    async def test_rendered_diagram_card_goes_to_the_feed(self, stand: Stand) -> None:
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

        stored = await get_bytes(stand.storage, stand.key("mermaid", "orders.mmd"))
        if stored.decode("utf-8") != ER_SPEC:
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
    ) -> None:
        files = ChatAttachments(
            call_stand.contexts,
            stand.storage,
            ChatMount(call_stand.contexts, runtime_stand.runs),
        )
        call = ToolCall(name="std_picture", args={}, id="call-9", type="tool_call")

        note = await files.attached(call, 0, "image/png", self.PNG)

        element = stand.layer.elements[0]
        if element.mime != "image/png":
            raise AssertionError(f"the file is attached to the chat: {element}")
        if not element.name.startswith("std_picture-"):
            raise AssertionError(f"the file is named after its tool: {element}")

        saved = await get_bytes(stand.storage, stand.key("upload", element.name))
        if saved != self.PNG:
            raise AssertionError(f"the block is a file of the thread: {saved!r}")
        if stand.upload_path(element.name) not in note:
            raise AssertionError(f"the model is told where the file is: {note}")

        shown = [shown[0] for shown in stand.turn.shown]
        if shown != ["call-9"]:
            raise AssertionError(f"the attachment belongs to its call: {shown}")

    @pytest.mark.anyio
    async def test_second_block_is_saved_without_a_second_attachment(
        self,
        stand: Stand,
        runtime_stand: StandRefs,
        call_stand: CallStand,
    ) -> None:
        files = ChatAttachments(
            call_stand.contexts,
            stand.storage,
            ChatMount(call_stand.contexts, runtime_stand.runs),
        )
        call = ToolCall(name="std_picture", args={}, id="call-9", type="tool_call")

        first = await files.attached(call, 0, "image/png", self.PNG)
        second = await files.attached(call, 1, "image/png", self.PNG)

        names = re.findall(r"std_picture-[^/\s]+\.png", first + second)
        if len(set(names)) != 2:
            raise AssertionError(f"every block has its own file: {first} {second}")

        for name in names:
            saved = await get_bytes(stand.storage, stand.key("upload", name))
            if saved != self.PNG:
                raise AssertionError(f"every block is saved: {name}")

        if len(stand.layer.elements) != 1:
            raise AssertionError(f"one attachment per call: {stand.layer.elements}")
