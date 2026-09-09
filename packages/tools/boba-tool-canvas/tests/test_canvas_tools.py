"""Тела канваса над временным workspace: пути треда, файлы, спека mermaid."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from boba.canvas.diagram import DiagramErrorKind
from boba.canvas.keys import WorkspaceRoot
from boba.identity.context import Scope, ScopeKind, Subject
from boba.tool.canvas.tools import (
    CanvasErrorKind,
    CanvasToolConfig,
    canvas_open,
    diagram_save,
    send_file,
)
from boba.toolkit.facade import PayloadTool
from boba.toolkit.result import CanvasResult, ErrorResult, FileResult

pytestmark = pytest.mark.anyio

THREAD = "11111111-1111-1111-1111-111111111111"
ER_SPEC = "erDiagram\n    CUSTOMER ||--o{ ORDER : has"


class Workspace:
    """Workspace пользователя во временном каталоге: корень, тред и субъект."""

    def __init__(self, tmp_path: Path) -> None:
        self.subject = Subject(
            user_id=uuid4(), login="tester", roles=frozenset(), profile="test"
        )
        self.scope = Scope(kind=ScopeKind.CHAT, id=THREAD)
        self.root = WorkspaceRoot(path=str(tmp_path / self.subject.user_key))
        self.thread = Path(self.root.path) / THREAD

    def upload(self, name: str, data: bytes) -> str:
        target = self.thread / "upload" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

        return str(target)


@pytest.fixture
def workspace(tmp_path: Path) -> Workspace:
    return Workspace(tmp_path)


async def _body(tool: PayloadTool, **kwargs: object) -> object:
    """Тело тула как функция: фасад хранит его в coroutine."""
    fn = tool.coroutine
    if fn is None:
        raise AssertionError(f"{tool.name} has no async body")

    return await fn(**kwargs)


class TestSendFile:
    async def test_existing_file_becomes_an_attachment(
        self, workspace: Workspace
    ) -> None:
        path = workspace.upload("report.pdf", b"%PDF-1.4")

        result = await _body(
            send_file,
            path=path,
            subject=workspace.subject,
            scope=workspace.scope,
            root=workspace.root,
        )

        if not isinstance(result, FileResult):
            raise AssertionError(result)

        if (result.path, result.name, result.mime) != (
            path,
            "report.pdf",
            "application/pdf",
        ):
            raise AssertionError(result)

    async def test_missing_file_is_refused(self, workspace: Workspace) -> None:
        path = str(workspace.thread / "upload" / "absent.txt")

        result = await _body(
            send_file,
            path=path,
            subject=workspace.subject,
            scope=workspace.scope,
            root=workspace.root,
        )

        if not isinstance(result, ErrorResult):
            raise AssertionError(result)

        if result.error_kind != CanvasErrorKind.FILE_NOT_FOUND:
            raise AssertionError(result)

    async def test_path_of_another_thread_is_refused(
        self, workspace: Workspace
    ) -> None:
        other = Path(workspace.root.path) / "22222222-2222-2222-2222-222222222222"
        other.joinpath("upload").mkdir(parents=True)
        path = other / "upload" / "leak.txt"
        path.write_text("x", encoding="utf-8")

        result = await _body(
            send_file,
            path=str(path),
            subject=workspace.subject,
            scope=workspace.scope,
            root=workspace.root,
        )

        if not isinstance(result, ErrorResult):
            raise AssertionError(result)

        if result.error_kind != CanvasErrorKind.BAD_PATH:
            raise AssertionError(result)


class TestCanvasOpen:
    async def test_existing_file_opens_in_the_canvas(
        self, workspace: Workspace
    ) -> None:
        path = workspace.upload("chart.png", b"\x89PNG")

        result = await _body(
            canvas_open,
            path=path,
            subject=workspace.subject,
            scope=workspace.scope,
            root=workspace.root,
        )

        if not isinstance(result, CanvasResult):
            raise AssertionError(result)

        if (result.path, result.label) != (path, "chart.png"):
            raise AssertionError(result)

        if "opened in the canvas: chart.png" not in result.llm_view():
            raise AssertionError(result.llm_view())

        if [item.item for item in result.chat_view().items] != ["panel"]:
            raise AssertionError(result.chat_view())


class TestDiagramSave:
    CFG = CanvasToolConfig(max_chars=1000)

    async def _save(self, workspace: Workspace, name: str, spec: str) -> object:
        return await _body(
            diagram_save,
            name=name,
            spec=spec,
            subject=workspace.subject,
            scope=workspace.scope,
            root=workspace.root,
            cfg=self.CFG,
        )

    async def test_spec_is_normalized_and_written(self, workspace: Workspace) -> None:
        result = await self._save(
            workspace, "orders.mmd", f"```mermaid\n{ER_SPEC}\n```"
        )

        if not isinstance(result, CanvasResult):
            raise AssertionError(result)

        stored = workspace.thread / "mermaid" / "orders.mmd"
        if result.path != str(stored):
            raise AssertionError(result.path)

        if stored.read_text(encoding="utf-8") != ER_SPEC:
            raise AssertionError(stored.read_text(encoding="utf-8"))

        if not result.llm_view().startswith(f"diagram saved: {stored}"):
            raise AssertionError(result.llm_view())

    async def test_bad_spec_is_refused(self, workspace: Workspace) -> None:
        result = await self._save(workspace, "x.mmd", "notadiagram\nA-->B")

        if not isinstance(result, ErrorResult):
            raise AssertionError(result)

        if result.error_kind != DiagramErrorKind.INVALID_SPEC:
            raise AssertionError(result)

    async def test_spec_over_the_limit_is_refused(self, workspace: Workspace) -> None:
        result = await self._save(workspace, "x.mmd", "erDiagram\n" + "A\n" * 600)

        if not isinstance(result, ErrorResult):
            raise AssertionError(result)

        if "character limit" not in result.message:
            raise AssertionError(result.message)

    async def test_traversal_in_name_stays_in_the_thread(
        self, workspace: Workspace
    ) -> None:
        result = await self._save(workspace, "../../etc/passwd", ER_SPEC)

        if not isinstance(result, CanvasResult):
            raise AssertionError(result)

        if not result.path.startswith(str(workspace.thread / "mermaid")):
            raise AssertionError(result.path)
