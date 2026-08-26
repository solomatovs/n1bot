"""Tool diagram_save и вьювер .mmd: разбор спеки, отказы, файл в storage."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, cast

import pytest
from conftest import FakeTurn, make_context, use_session
from pydantic import BaseModel

from boba.canvas.canvas import (
    CanvasError,
    CanvasErrorKind,
    CanvasRegistry,
    RenderStatus,
    RenderVerdicts,
)
from boba.canvas.diagram import (
    DiagramEntry,
    DiagramErrorKind,
    DiagramRefusedError,
    DiagramSpecError,
    DiagramToolConfig,
    MermaidSpec,
)
from boba.canvas.keys import ObjectKey, ThreadDir
from boba.chainlit.agent.toolrun.call_id import ToolCallIdField
from boba.chainlit.agent.toolrun.run_log import ToolRunLogger
from boba.chainlit.canvas import diagram as diagram_module
from boba.chainlit.canvas.diagram import (
    DiagramFiles,
    MermaidViewer,
    build_diagram_tools,
)
from boba.chainlit.canvas.panel import CanvasPanel
from boba.chainlit.data.data_layer import AttachmentDataLayer
from boba.chainlit.data.storage import LocalStorageClient
from boba.chainlit.infra.config import LocalStorageConfig
from boba.chainlit.infra.plugins import tool_call_scope
from boba.identity.context import ContextKind
from boba.identity.errors import RefusalError
from boba.identity.run import RunRegistry
from boba.toolkit.result import DiagramResult, ErrorResult, TextResult
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


class TestToolInterface:
    def test_tool_names(self) -> None:
        tools = build_diagram_tools(DiagramToolConfig(max_chars=1000))
        if [t.name for t in tools] != ["diagram_save"]:
            raise AssertionError('[t.name for t in tools] == ["diagram_save"]')

    def test_save_schema_fields(self) -> None:
        save = build_diagram_tools(DiagramToolConfig(max_chars=1000))[0]
        schema = cast(type[BaseModel], save.tool_call_schema)
        if set(schema.model_fields) != {"name", "spec"}:
            raise AssertionError('set(schema.model_fields) == {"name", "spec"}')

    def test_build_registers_viewer(self) -> None:
        """Канвас узнаёт про .mmd только отсюда — иначе файл некому показать."""
        CanvasRegistry.reset()
        build_diagram_tools(DiagramToolConfig(max_chars=1000))

        viewer = CanvasRegistry.viewer_for("orders.mmd")

        if not (isinstance(viewer, MermaidViewer)):
            raise AssertionError("isinstance(viewer, MermaidViewer)")
        if CanvasRegistry.viewer_for("notes.txt") is not None:
            raise AssertionError('CanvasRegistry.viewer_for("notes.txt") is None')


class TestRefusal:
    """Отказ доезжает до LLM ошибкой с причиной, а не исключением."""

    @pytest.mark.anyio
    async def test_save_without_session(self) -> None:
        with pytest.raises(RefusalError) as failure:
            await DiagramFiles(1000).save("x.mmd", ER_SPEC)

        if failure.value.kind != ContextKind.NO_CONTEXT:
            raise AssertionError("failure.value.kind == ContextKind.NO_CONTEXT")

    @pytest.mark.anyio
    async def test_save_bad_spec(self, monkeypatch: pytest.MonkeyPatch) -> None:
        use_session(monkeypatch, user_id="7", thread_id=THREAD)

        with pytest.raises(DiagramRefusedError) as failure:
            await DiagramFiles(1000).save("x.mmd", "не mermaid вовсе")

        if failure.value.kind != DiagramErrorKind.INVALID_SPEC:
            raise AssertionError("failure.value.kind == DiagramErrorKind.INVALID_SPEC")

    @pytest.mark.anyio
    async def test_save_over_limit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        use_session(monkeypatch, user_id="7", thread_id=THREAD)

        with pytest.raises(DiagramRefusedError) as failure:
            await DiagramFiles(10).save("x.mmd", ER_SPEC)

        if failure.value.kind != DiagramErrorKind.INVALID_SPEC:
            raise AssertionError("failure.value.kind == DiagramErrorKind.INVALID_SPEC")

    @pytest.mark.anyio
    async def test_save_path_traversal_in_name(
        self, monkeypatch: pytest.MonkeyPatch, files: DiagramFiles
    ) -> None:
        """Имя от LLM чистится: файл остаётся в каталоге диаграмм треда."""
        key = await files.save("../../etc/passwd.mmd", ER_SPEC)

        if key.in_workspace() != f"/workspace/{THREAD}/mermaid/passwd.mmd":
            raise AssertionError('key.in_workspace() == f"/workspace/{THREAD}/mermaid…')


class _StorageOnlyLayer:
    """Доступ тулов к слою в тесте: из всего слоя нужен только storage."""

    def __init__(self, storage: LocalStorageClient) -> None:
        self.storage = storage


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

    use_session(monkeypatch, user_id="7", thread_id=THREAD)
    monkeypatch.setattr(AttachmentDataLayer, "require", classmethod(lambda cls: layer))

    return DiagramFiles(1000)


class TestSaveAndView:
    """Файл проходит цикл целиком: сохранение, чтение, показ вьювером."""

    @pytest.mark.anyio
    async def test_save_writes_normalized_file(
        self, files: DiagramFiles, tmp_path: Path
    ) -> None:
        key = await files.save("orders.mmd", f"```mermaid\n{ER_SPEC}\n```")

        if key.in_workspace() != f"/workspace/{THREAD}/mermaid/orders.mmd":
            raise AssertionError('key.in_workspace() == f"/workspace/{THREAD}/mermaid…')

        stored = tmp_path / "7" / THREAD / "mermaid" / "orders.mmd"
        if stored.read_text(encoding="utf-8") != ER_SPEC:
            raise AssertionError('stored.read_text(encoding="utf-8") == ER_SPEC')

    @pytest.fixture
    def fast_verdict(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """В тестах браузера нет: ожидание вердикта срезается до мгновенного."""
        monkeypatch.setattr(MermaidViewer, "VERDICT_TIMEOUT_SEC", 0.05)

    @pytest.mark.anyio
    async def test_viewer_shows_saved_file(
        self, files: DiagramFiles, http_context: None, fast_verdict: None
    ) -> None:
        await files.save("orders.mmd", ER_SPEC)

        shown: list[Any] = []

        async def push(content: Any) -> None:
            shown.append(content)

        key = ObjectKey.build(
            "7", THREAD, "orders.mmd", "el-1", dir_thread=ThreadDir.MERMAID
        )
        opened = await MermaidViewer(files).open(key, push)

        if opened.label != "orders.mmd":
            raise AssertionError('opened.label == "orders.mmd"')
        if not (isinstance(opened.link, DiagramResult)):
            raise AssertionError("isinstance(opened.link, DiagramResult)")
        if opened.link.spec != ER_SPEC:
            raise AssertionError("opened.link.spec == ER_SPEC")
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
        storage = AttachmentDataLayer.require().storage
        await storage.upload_file(
            object_key=f"7/{THREAD}/upload/mine.mmd",
            data=ER_SPEC,
            mime="text/plain",
        )

        shown: list[Any] = []

        async def push(content: Any) -> None:
            shown.append(content)

        key = ObjectKey.build(
            "7", THREAD, "mine.mmd", "el-1", dir_thread=ThreadDir.UPLOAD
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
            "7", THREAD, "no.mmd", "el-1", dir_thread=ThreadDir.MERMAID
        )

        with pytest.raises(DiagramRefusedError) as failure:
            await files.read(key)

        if failure.value.kind != DiagramErrorKind.FILE_NOT_FOUND:
            raise AssertionError("failure.value.kind == DiagramErrorKind.FILE_NOT_FOU…")


class TestEntry:
    """Метаданные диаграммы: подпись и тип для панели и ленты."""

    FLOW_SPEC = "---\ntitle: Процесс\n---\nflowchart LR\n    A --> B"

    def test_entry_of_unparsed_spec_keeps_text(self) -> None:
        """Файл не mermaid: текст едет как есть, метаданных нет, подпись — имя."""
        key = ObjectKey.build(
            "7", THREAD, "a.mmd", "el-1", dir_thread=ThreadDir.MERMAID
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
            "7", THREAD, "a.mmd", "el-1", dir_thread=ThreadDir.MERMAID
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
            object_key=f"7/{THREAD}/mermaid/bin.mmd",
            data=b"\xff\xfe\x00\x01",
            mime="application/octet-stream",
        )

        key = ObjectKey.build(
            "7", THREAD, "bin.mmd", "el-1", dir_thread=ThreadDir.MERMAID
        )

        with pytest.raises(DiagramRefusedError) as failure:
            await files.read(key)

        if failure.value.kind != DiagramErrorKind.BAD_FILE:
            raise AssertionError("failure.value.kind == DiagramErrorKind.BAD_FILE")

    @pytest.mark.anyio
    async def test_read_refuses_file_over_the_limit(self, files: DiagramFiles) -> None:
        """Потолок на объём держит тул: хранилище отдаёт что угодно потоком.

        Файл в mermaid/ пишет bash, поэтому он может быть сколь угодно велик,
        а спека целиком уезжает в props элемента и в LLM.
        """
        storage = AttachmentDataLayer.require().storage
        oversized = "flowchart LR\n" + "  A --> B\n" * 4000
        await storage.upload_file(
            object_key=f"7/{THREAD}/mermaid/huge.mmd",
            data=oversized,
            mime="text/plain",
        )

        key = ObjectKey.build(
            "7", THREAD, "huge.mmd", "el-1", dir_thread=ThreadDir.MERMAID
        )

        with pytest.raises(DiagramRefusedError) as failure:
            await files.read(key)

        if failure.value.kind != DiagramErrorKind.BAD_FILE:
            raise AssertionError("failure.value.kind == DiagramErrorKind.BAD_FILE")


class TestWatchSource:
    """Слежение за спекой: сигнал по смене содержимого, битый тик пропускается."""

    @pytest.mark.anyio
    async def test_probe_changes_only_on_new_content(
        self, files: DiagramFiles, http_context: None
    ) -> None:
        await files.save("orders.mmd", ER_SPEC)
        key = ObjectKey.build(
            "7", THREAD, "orders.mmd", "el-1", dir_thread=ThreadDir.MERMAID
        )

        source = MermaidViewer(files).watch_source(key)
        if source is None:
            raise AssertionError("source is not None")

        first = await source.probe()
        same = await source.probe()

        await files.save("orders.mmd", ER_SPEC + "\n  C ||--o{ D : owns")
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
        await files.save("orders.mmd", ER_SPEC)
        key = ObjectKey.build(
            "7", THREAD, "orders.mmd", "el-1", dir_thread=ThreadDir.MERMAID
        )

        source = MermaidViewer(files).watch_source(key)
        if source is None:
            raise AssertionError("source is not None")

        first = await source.probe()

        missing = ObjectKey.build(
            "7", THREAD, "absent.mmd", "el-1", dir_thread=ThreadDir.MERMAID
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
        await files.save("orders.mmd", ER_SPEC)
        key = ObjectKey.build(
            "7", THREAD, "orders.mmd", "el-1", dir_thread=ThreadDir.MERMAID
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


class TestSaveToolEndToEnd:
    """diagram_save целиком: сохранить, карточку в ленту, вердикт — с панели.

    Во время хода смонтирована только панель, поэтому показ в ней и есть
    верификация спеки; карточка уходит без nonce и вердикт не репортит.
    """

    @pytest.fixture(autouse=True)
    def active_turn(self) -> Any:
        """Карточка цепляется к шагу ответа: без живого хода её некуда деть."""
        scope = RunRegistry.open(make_context(THREAD), cast(Any, FakeTurn()))
        scope.__enter__()
        yield
        scope.__exit__(None, None, None)

    @pytest.fixture
    def feed(self, monkeypatch: pytest.MonkeyPatch) -> list[Any]:
        """Лента под тест: собирает карточки, ушедшие бы во фронт."""
        shown: list[Any] = []

        async def capture(self: Any, for_id: str | None = None) -> None:
            shown.append(self)

        monkeypatch.setattr(diagram_module.cl.CustomElement, "send", capture)
        return shown

    @pytest.fixture
    def panel(self, monkeypatch: pytest.MonkeyPatch) -> list[Any]:
        """Панель под тест: собирает содержимое, ушедшее бы в side view."""
        pushed: list[Any] = []

        async def capture(cls: Any, content: Any) -> None:
            pushed.append(content)

        monkeypatch.setattr(CanvasPanel, "_push", classmethod(capture))
        return pushed

    async def _call(self, spec: str, verdict: dict[str, Any], panel: list[Any]) -> Any:
        """Зовёт тул как агент — tool_call, иначе artifact до вызывающего не дойдёт.

        Та же обвязка, что ставит load_tools: id вызова со схемы уходит в
        контекст, и карточка получает адрес элемента по нему.
        """
        save = build_diagram_tools(DiagramToolConfig(max_chars=32000))[0]
        ToolCallIdField.attach_all([save])
        ToolRunLogger.guard_all([save], lambda tool, call_id: None, tool_call_scope)
        request = {
            "name": "diagram_save",
            "args": {"name": "orders.mmd", "spec": spec},
            "id": "call-1",
            "type": "tool_call",
        }
        call = asyncio.ensure_future(save.ainvoke(request))

        await asyncio.wait_for(self._await_push(panel), 5)

        RenderVerdicts.report({"nonce": panel[0].nonce, **verdict})
        message = await asyncio.wait_for(call, 5)

        return message.content, message.artifact

    @staticmethod
    async def _await_push(pushed: list[Any]) -> None:
        while not pushed:
            await asyncio.sleep(0.01)

    @pytest.mark.anyio
    async def test_render_failure_becomes_tool_error(
        self,
        files: DiagramFiles,
        http_context: None,
        feed: list[Any],
        panel: list[Any],
    ) -> None:
        """Битую спеку ловит только браузер — LLM обязана узнать об этом."""
        _, result = await self._call(
            ER_SPEC, {"ok": False, "error": "Parse error on line 5"}, panel
        )

        if not (isinstance(result, ErrorResult)):
            raise AssertionError("isinstance(result, ErrorResult)")
        if result.error_kind != CanvasErrorKind.RENDER_FAILED:
            raise AssertionError("result.error_kind == CanvasErrorKind.RENDER_FAILED")
        if "Parse error on line 5" not in result.message:
            raise AssertionError('"Parse error on line 5" in result.message')
        if "diagram saved" not in result.message:
            raise AssertionError('"diagram saved" in result.message')

    @pytest.mark.anyio
    async def test_failed_diagram_leaves_no_card_in_the_feed(
        self,
        files: DiagramFiles,
        http_context: None,
        feed: list[Any],
        panel: list[Any],
    ) -> None:
        """Неотрисованная спека в переписке не остаётся: её правят следующим
        вызовом, а попытка видна шагом инструмента внутри хода."""
        await self._call(ER_SPEC, {"ok": False, "error": "Parse error"}, panel)

        if feed != []:
            raise AssertionError("feed == []")

    @pytest.mark.anyio
    async def test_rendered_diagram_card_goes_to_the_feed(
        self,
        files: DiagramFiles,
        http_context: None,
        feed: list[Any],
        panel: list[Any],
    ) -> None:
        """Успех — диаграмма в панели плюс кликабельная карточка в ленте."""
        content, result = await self._call(ER_SPEC, {"ok": True, "error": ""}, panel)

        if not (isinstance(result, TextResult)):
            raise AssertionError("isinstance(result, TextResult)")
        if "diagram saved" not in content:
            raise AssertionError('"diagram saved" in content')

        if panel[0].kind != "mermaid":
            raise AssertionError('panel[0].kind == "mermaid"')
        if panel[0].text != ER_SPEC:
            raise AssertionError("panel[0].text == ER_SPEC")

        card = feed[0]
        if card.props["kind"] != "mermaid":
            raise AssertionError('card.props["kind"] == "mermaid"')
        if card.props["preview"] is not True:
            raise AssertionError('card.props["preview"] is True')
        if card.props["text"] != ER_SPEC:
            raise AssertionError('card.props["text"] == ER_SPEC')
        if card.props.get("nonce"):
            raise AssertionError("карточка не участвует в верификации")
        if card.props["path"] != f"/workspace/{THREAD}/mermaid/orders.mmd":
            raise AssertionError('card.props["path"] == f"/workspace/{THREAD}/mermaid…')
