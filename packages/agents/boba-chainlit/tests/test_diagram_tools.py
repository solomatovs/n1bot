"""Вьювер .mmd канваса: разбор спеки, чтение из storage, вердикт рендера."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from chainlit_stand import use_session

from boba.canvas.canvas import (
    CanvasError,
    CanvasErrorKind,
    RenderStatus,
    RenderVerdicts,
)
from boba.canvas.diagram import DiagramEntry, DiagramSpecError, MermaidSpec
from boba.canvas.keys import ObjectKey, ThreadDir
from boba.chainlit.canvas.diagram import DiagramFiles, MermaidViewer
from boba.chainlit.data.data_layer import AttachmentDataLayer
from boba.chainlit.data.storage import LocalStorageClient
from boba.chainlit.infra.config import LocalStorageConfig
from boba.toolkit.result import VisualResult
from boba.workspace.binaries import TrustedBinaries
from boba.workspace.launcher import MountingConfig

THREAD = "11111111-1111-1111-1111-111111111111"
ER_SPEC = "erDiagram\n    CUSTOMER ||--o{ ORDER : has"


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    """Заглушка сессионной фикстуры conftest: БД этим тестам не нужна."""


@pytest.fixture
async def http_context() -> None:
    """cl.CustomElement требует контекст chainlit и живой цикл событий."""
    from chainlit.context import init_http_context

    init_http_context()


class TestMermaidSpec:
    def test_plain_spec(self) -> None:
        parsed = MermaidSpec.parse(ER_SPEC)
        if parsed.diagram_type != "erDiagram":
            raise AssertionError('parsed.diagram_type == "erDiagram"')
        if parsed.title is not None:
            raise AssertionError("parsed.title is None")
        if parsed.text != ER_SPEC:
            raise AssertionError("parsed.text == ER_SPEC")

    def test_fence_with_language_is_stripped(self) -> None:
        parsed = MermaidSpec.parse(f"```mermaid\n{ER_SPEC}\n```")
        if parsed.text != ER_SPEC:
            raise AssertionError("parsed.text == ER_SPEC")

    def test_bare_fence_is_stripped(self) -> None:
        parsed = MermaidSpec.parse(f"```\n{ER_SPEC}\n```")
        if parsed.text != ER_SPEC:
            raise AssertionError("parsed.text == ER_SPEC")

    def test_indented_spec_is_dedented(self) -> None:
        raw = "    erDiagram\n        A ||--o{ B : x"
        parsed = MermaidSpec.parse(raw)
        if not (parsed.text.startswith("erDiagram")):
            raise AssertionError('parsed.text.startswith("erDiagram")')

    def test_frontmatter_title_extracted_and_kept(self) -> None:
        raw = f"---\ntitle: Схема заказов\n---\n{ER_SPEC}"
        parsed = MermaidSpec.parse(raw)
        if parsed.title != "Схема заказов":
            raise AssertionError('parsed.title == "Схема заказов"')
        if parsed.diagram_type != "erDiagram":
            raise AssertionError('parsed.diagram_type == "erDiagram"')
        if not (parsed.text.startswith("---")):
            raise AssertionError('parsed.text.startswith("---")')

    def test_comment_lines_are_skipped(self) -> None:
        parsed = MermaidSpec.parse(f"%% комментарий\n{ER_SPEC}")
        if parsed.diagram_type != "erDiagram":
            raise AssertionError('parsed.diagram_type == "erDiagram"')

    def test_dashed_type_token(self) -> None:
        parsed = MermaidSpec.parse("stateDiagram-v2\n    [*] --> Active")
        if parsed.diagram_type != "stateDiagram-v2":
            raise AssertionError('parsed.diagram_type == "stateDiagram-v2"')

    def test_unknown_type_rejected_with_known_list(self) -> None:
        with pytest.raises(DiagramSpecError, match="erDiagram"):
            MermaidSpec.parse("plantuml\nA -> B")

    def test_empty_spec_rejected(self) -> None:
        with pytest.raises(DiagramSpecError, match="empty"):
            MermaidSpec.parse("```mermaid\n```")


class _StorageOnlyLayer:
    """Доступ тулов к слою в тесте: storage и запись элементов, которые уходят в
    ленту.
    """

    def __init__(self, storage: LocalStorageClient) -> None:
        self.storage = storage
        self.elements: list[Any] = []

    async def create_element(self, element: Any) -> None:
        self.elements.append(element)


@pytest.fixture
def files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> DiagramFiles:
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
    storage = LocalStorageClient(config)
    layer = _StorageOnlyLayer(storage)

    use_session(monkeypatch, user_id=str(UUID(int=7)), thread_id=THREAD)
    monkeypatch.setattr(AttachmentDataLayer, "require", classmethod(lambda cls: layer))

    return DiagramFiles()


async def _stored(
    name: str, text: str, dir_thread: ThreadDir = ThreadDir.MERMAID
) -> None:
    """Спека в storage тем путём, каким её кладёт тело diagram_save."""
    storage = AttachmentDataLayer.require().storage
    await storage.upload_file(
        object_key=f"{UUID(int=7)}/{THREAD}/{dir_thread.value}/{name}",
        data=text,
        mime="text/plain",
        overwrite=True,
    )


class TestSaveAndView:
    """Сохранённый файл читается и показывается вьювером."""

    @pytest.fixture
    def fast_verdict(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """В тестах браузера нет: ожидание вердикта срезается до мгновенного."""
        monkeypatch.setattr(MermaidViewer, "VERDICT_TIMEOUT_SEC", 0.05)

    @pytest.mark.anyio
    async def test_viewer_shows_saved_file(
        self, files: DiagramFiles, http_context: None, fast_verdict: None
    ) -> None:
        await _stored("orders.mmd", ER_SPEC)

        shown: list[Any] = []

        async def push(content: Any) -> None:
            shown.append(content)

        key = ObjectKey.build(
            str(UUID(int=7)), THREAD, "orders.mmd", "el-1", dir_thread=ThreadDir.MERMAID
        )
        opened = await MermaidViewer(files).open(key, push)

        if opened.label != "orders.mmd":
            raise AssertionError('opened.label == "orders.mmd"')
        if not (isinstance(opened.link, VisualResult)):
            raise AssertionError("isinstance(opened.link, VisualResult)")
        if opened.link.props["text"] != ER_SPEC:
            raise AssertionError('opened.link.props["text"] == ER_SPEC')
        if len(shown) != 1:
            raise AssertionError("len(shown) == 1")
        content = shown[0]
        if content.path != key.in_workspace():
            raise AssertionError("content.path == key.in_workspace()")
        if content.text != ER_SPEC:
            raise AssertionError("content.text == ER_SPEC")
        if not (content.nonce):
            raise AssertionError("content.nonce")

    @pytest.mark.anyio
    async def test_viewer_reads_user_upload(
        self, files: DiagramFiles, http_context: None, fast_verdict: None
    ) -> None:
        """Пользовательский .mmd из upload/ показывается тем же вьювером."""
        await _stored("mine.mmd", ER_SPEC, ThreadDir.UPLOAD)

        shown: list[Any] = []

        async def push(content: Any) -> None:
            shown.append(content)

        key = ObjectKey.build(
            str(UUID(int=7)), THREAD, "mine.mmd", "el-1", dir_thread=ThreadDir.UPLOAD
        )
        await MermaidViewer(files).open(key, push)

        if shown[0].text != ER_SPEC:
            raise AssertionError("shown[0].text == ER_SPEC")

    def test_viewer_handles_only_mmd(self, files: DiagramFiles) -> None:
        viewer = MermaidViewer(files)

        if viewer.handles("orders.mmd") is not True:
            raise AssertionError('viewer.handles("orders.mmd") is True')
        if viewer.handles("report.pdf") is not False:
            raise AssertionError('viewer.handles("report.pdf") is False')

    @pytest.mark.anyio
    async def test_read_missing_file(self, files: DiagramFiles) -> None:
        key = ObjectKey.build(
            str(UUID(int=7)), THREAD, "no.mmd", "el-1", dir_thread=ThreadDir.MERMAID
        )

        with pytest.raises(CanvasError) as failure:
            await files.read(key)

        if failure.value.kind != CanvasErrorKind.FILE_NOT_FOUND:
            raise AssertionError(failure.value.kind)


class TestEntry:
    """Метаданные диаграммы: подпись и тип для панели и ленты."""

    FLOW_SPEC = "---\ntitle: Процесс\n---\nflowchart LR\n    A --> B"

    def test_entry_of_unparsed_spec_keeps_text(self) -> None:
        """Файл не mermaid: текст едет как есть, метаданных нет, подпись — имя."""
        key = ObjectKey.build(
            str(UUID(int=7)), THREAD, "a.mmd", "el-1", dir_thread=ThreadDir.MERMAID
        )

        entry = DiagramEntry.of(key, "не диаграмма вовсе")

        if entry.spec != "не диаграмма вовсе":
            raise AssertionError('entry.spec == "не диаграмма вовсе"')
        if entry.type != "":
            raise AssertionError('entry.type == ""')
        if entry.label != "a.mmd":
            raise AssertionError('entry.label == "a.mmd"')

    def test_entry_of_broken_body_keeps_type(self) -> None:
        """Заголовок разобран — тип известен; синтаксис тела проверяет браузер."""
        key = ObjectKey.build(
            str(UUID(int=7)), THREAD, "a.mmd", "el-1", dir_thread=ThreadDir.MERMAID
        )

        entry = DiagramEntry.of(key, "erDiagram\n  A ||--")

        if entry.spec != "erDiagram\n  A ||--":
            raise AssertionError('entry.spec == "erDiagram\\n A ||--"')
        if entry.type != "erDiagram":
            raise AssertionError('entry.type == "erDiagram"')

    @pytest.mark.anyio
    async def test_read_binary_file(self, files: DiagramFiles) -> None:
        storage = AttachmentDataLayer.require().storage
        await storage.upload_file(
            object_key=f"{UUID(int=7)}/{THREAD}/mermaid/bin.mmd",
            data=b"\xff\xfe\x00\x01",
            mime="application/octet-stream",
        )

        key = ObjectKey.build(
            str(UUID(int=7)), THREAD, "bin.mmd", "el-1", dir_thread=ThreadDir.MERMAID
        )

        with pytest.raises(CanvasError) as failure:
            await files.read(key)

        if failure.value.kind != CanvasErrorKind.BAD_FILE:
            raise AssertionError("failure.value.kind == CanvasErrorKind.BAD_FILE")

    @pytest.mark.anyio
    async def test_read_refuses_file_over_the_limit(self, files: DiagramFiles) -> None:
        """Потолок на объём держит тул: хранилище отдаёт что угодно потоком.

        Файл в mermaid/ пишет bash, поэтому он может быть сколь угодно велик,
        а спека целиком уезжает в props элемента и в LLM.
        """
        oversized = "flowchart LR\n" + "  A --> B\n" * 4000
        await _stored("huge.mmd", oversized)

        key = ObjectKey.build(
            str(UUID(int=7)), THREAD, "huge.mmd", "el-1", dir_thread=ThreadDir.MERMAID
        )

        with pytest.raises(CanvasError) as failure:
            await DiagramFiles(max_bytes=1000).read(key)

        if failure.value.kind != CanvasErrorKind.TOO_LARGE:
            raise AssertionError(failure.value.kind)


class TestWatchSource:
    """Слежение за спекой: сигнал по смене содержимого, битый тик пропускается."""

    @pytest.mark.anyio
    async def test_probe_changes_only_on_new_content(
        self, files: DiagramFiles, http_context: None
    ) -> None:
        await _stored("orders.mmd", ER_SPEC)
        key = ObjectKey.build(
            str(UUID(int=7)), THREAD, "orders.mmd", "el-1", dir_thread=ThreadDir.MERMAID
        )

        source = MermaidViewer(files).watch_source(key)
        if source is None:
            raise AssertionError("source is not None")

        first = await source.probe()
        same = await source.probe()

        await _stored("orders.mmd", ER_SPEC + "\n  C ||--o{ D : owns")
        changed = await source.probe()

        if first is None or same is None or changed is None:
            raise AssertionError("first is not None and same and changed")
        if first.revision != same.revision:
            raise AssertionError("first.revision == same.revision")
        if changed.revision == first.revision:
            raise AssertionError("changed.revision != first.revision")

    @pytest.mark.anyio
    async def test_read_error_keeps_the_last_probe(
        self, files: DiagramFiles, http_context: None
    ) -> None:
        """Файл в момент чтения переписывается — тик отдаёт прежнее состояние."""
        await _stored("orders.mmd", ER_SPEC)
        key = ObjectKey.build(
            str(UUID(int=7)), THREAD, "orders.mmd", "el-1", dir_thread=ThreadDir.MERMAID
        )

        source = MermaidViewer(files).watch_source(key)
        if source is None:
            raise AssertionError("source is not None")

        first = await source.probe()

        missing = ObjectKey.build(
            str(UUID(int=7)), THREAD, "absent.mmd", "el-1", dir_thread=ThreadDir.MERMAID
        )
        broken = MermaidViewer(files).watch_source(missing)
        if broken is None:
            raise AssertionError("broken is not None")

        if await broken.probe() is not None:
            raise AssertionError("await broken.probe() is None")
        if first is None:
            raise AssertionError("first is not None")


class TestRenderVerdicts:
    """Вердикт браузера: отчёт находит ожидание по nonce, молчание — UNKNOWN."""

    @pytest.mark.anyio
    async def test_report_resolves_waiter(self) -> None:
        RenderVerdicts.expect("n-1")

        RenderVerdicts.report({"nonce": "n-1", "ok": False, "error": "Parse error"})
        verdict = await RenderVerdicts.wait("n-1", 1.0)

        if verdict.status is not RenderStatus.FAILED:
            raise AssertionError("verdict.status is RenderStatus.FAILED")
        if verdict.message != "Parse error":
            raise AssertionError('verdict.message == "Parse error"')

    @pytest.mark.anyio
    async def test_success_report(self) -> None:
        RenderVerdicts.expect("n-2")

        RenderVerdicts.report({"nonce": "n-2", "ok": True, "error": ""})
        verdict = await RenderVerdicts.wait("n-2", 1.0)

        if verdict.status is not RenderStatus.RENDERED:
            raise AssertionError("verdict.status is RenderStatus.RENDERED")

    @pytest.mark.anyio
    async def test_silence_is_unknown(self) -> None:
        RenderVerdicts.expect("n-3")

        verdict = await RenderVerdicts.wait("n-3", 0.05)

        if verdict.status is not RenderStatus.UNKNOWN:
            raise AssertionError("verdict.status is RenderStatus.UNKNOWN")

    @pytest.mark.anyio
    async def test_unknown_nonce_is_ignored(self) -> None:
        RenderVerdicts.report({"nonce": "missing", "ok": True, "error": ""})


class TestViewerVerdict:
    """FAILED от браузера превращается в CanvasError с текстом mermaid."""

    @pytest.mark.anyio
    async def test_render_failure_raises(
        self, files: DiagramFiles, http_context: None
    ) -> None:
        await _stored("orders.mmd", ER_SPEC)
        key = ObjectKey.build(
            str(UUID(int=7)), THREAD, "orders.mmd", "el-1", dir_thread=ThreadDir.MERMAID
        )

        shown: list[Any] = []

        async def push(content: Any) -> None:
            shown.append(content)

        opening = asyncio.ensure_future(MermaidViewer(files).open(key, push))

        while not shown:
            await asyncio.sleep(0.01)

        nonce = shown[0].nonce
        RenderVerdicts.report(
            {"nonce": nonce, "ok": False, "error": "Parse error on line 5"}
        )

        with pytest.raises(CanvasError) as failure:
            await opening

        if failure.value.kind != CanvasErrorKind.RENDER_FAILED:
            raise AssertionError("failure.value.kind == CanvasErrorKind.RENDER_FAILED")
        if "Parse error on line 5" not in str(failure.value):
            raise AssertionError('"Parse error on line 5" in str(failure.value)')
