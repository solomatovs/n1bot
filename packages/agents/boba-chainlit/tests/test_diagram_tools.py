"""Tool diagram_save и вьювер .mmd: разбор спеки, отказы, файл в storage."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import BaseModel

from boba.chainlit.agent.tools import diagram as diagram_module
from boba.chainlit.agent.tools.diagram import (
    CanvasWatcher,
    DiagramEntry,
    DiagramErrorKind,
    DiagramFiles,
    DiagramRefusedError,
    DiagramSpecError,
    DiagramToolConfig,
    MermaidSpec,
    MermaidViewer,
    build_diagram_tools,
)
from boba.chainlit.data.storage import LocalStorageClient
from boba.chainlit.domain import session as session_module
from boba.chainlit.domain.errors import RefusalError
from boba.chainlit.domain.keys import ObjectKey, ThreadDir
from boba.chainlit.domain.session import SessionKind
from boba.chainlit.domain.turn import TurnContext
from boba.chainlit.infra.config import LocalStorageConfig
from boba.chainlit.rendering.canvas import (
    CanvasError,
    CanvasErrorKind,
    CanvasPanel,
    CanvasRegistry,
    RenderStatus,
    RenderVerdicts,
)
from boba.toolkit.binaries import TrustedBinaries
from boba.toolkit.result import DiagramResult, ErrorResult, TextResult
from boba.workspace.launcher import LauncherConfig

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
        assert parsed.diagram_type == "erDiagram"
        assert parsed.title is None
        assert parsed.text == ER_SPEC

    def test_fence_with_language_is_stripped(self) -> None:
        parsed = MermaidSpec.parse(f"```mermaid\n{ER_SPEC}\n```")
        assert parsed.text == ER_SPEC

    def test_bare_fence_is_stripped(self) -> None:
        parsed = MermaidSpec.parse(f"```\n{ER_SPEC}\n```")
        assert parsed.text == ER_SPEC

    def test_indented_spec_is_dedented(self) -> None:
        raw = "    erDiagram\n        A ||--o{ B : x"
        parsed = MermaidSpec.parse(raw)
        assert parsed.text.startswith("erDiagram")

    def test_frontmatter_title_extracted_and_kept(self) -> None:
        raw = f"---\ntitle: Схема заказов\n---\n{ER_SPEC}"
        parsed = MermaidSpec.parse(raw)
        assert parsed.title == "Схема заказов"
        assert parsed.diagram_type == "erDiagram"
        assert parsed.text.startswith("---")

    def test_comment_lines_are_skipped(self) -> None:
        parsed = MermaidSpec.parse(f"%% комментарий\n{ER_SPEC}")
        assert parsed.diagram_type == "erDiagram"

    def test_dashed_type_token(self) -> None:
        parsed = MermaidSpec.parse("stateDiagram-v2\n    [*] --> Active")
        assert parsed.diagram_type == "stateDiagram-v2"

    def test_unknown_type_rejected_with_known_list(self) -> None:
        with pytest.raises(DiagramSpecError, match="erDiagram"):
            MermaidSpec.parse("plantuml\nA -> B")

    def test_empty_spec_rejected(self) -> None:
        with pytest.raises(DiagramSpecError, match="empty"):
            MermaidSpec.parse("```mermaid\n```")


class TestToolInterface:
    def test_tool_names(self) -> None:
        tools = build_diagram_tools(DiagramToolConfig(max_chars=1000))
        assert [t.name for t in tools] == ["diagram_save"]

    def test_save_schema_fields(self) -> None:
        save = build_diagram_tools(DiagramToolConfig(max_chars=1000))[0]
        schema = cast(type[BaseModel], save.tool_call_schema)
        assert set(schema.model_fields) == {"name", "spec"}

    def test_build_registers_viewer(self) -> None:
        """Канвас узнаёт про .mmd только отсюда — иначе файл некому показать."""
        CanvasRegistry.reset()
        build_diagram_tools(DiagramToolConfig(max_chars=1000))

        viewer = CanvasRegistry.viewer_for("orders.mmd")

        assert isinstance(viewer, MermaidViewer)
        assert CanvasRegistry.viewer_for("notes.txt") is None


class TestRefusal:
    """Отказ доезжает до LLM ошибкой с причиной, а не исключением."""

    @pytest.mark.anyio
    async def test_save_without_session(self) -> None:
        with pytest.raises(RefusalError) as failure:
            await DiagramFiles(1000).save("x.mmd", ER_SPEC)

        assert failure.value.kind == SessionKind.NO_SESSION

    @pytest.mark.anyio
    async def test_save_bad_spec(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(session_module, "current_user_id", lambda: "7")
        monkeypatch.setattr(session_module, "current_thread_id", lambda: THREAD)

        with pytest.raises(DiagramRefusedError) as failure:
            await DiagramFiles(1000).save("x.mmd", "не mermaid вовсе")

        assert failure.value.kind == DiagramErrorKind.INVALID_SPEC

    @pytest.mark.anyio
    async def test_save_over_limit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(session_module, "current_user_id", lambda: "7")
        monkeypatch.setattr(session_module, "current_thread_id", lambda: THREAD)

        with pytest.raises(DiagramRefusedError) as failure:
            await DiagramFiles(10).save("x.mmd", ER_SPEC)

        assert failure.value.kind == DiagramErrorKind.INVALID_SPEC

    @pytest.mark.anyio
    async def test_save_path_traversal_in_name(
        self, monkeypatch: pytest.MonkeyPatch, files: DiagramFiles
    ) -> None:
        """Имя от LLM чистится: файл остаётся в каталоге диаграмм треда."""
        key = await files.save("../../etc/passwd.mmd", ER_SPEC)

        assert key.in_workspace() == f"/workspace/{THREAD}/mermaid/passwd.mmd"


class _StorageOnlyLayer:
    """Доступ тулов к слою в тесте: из всего слоя нужен только storage."""

    def __init__(self, storage: LocalStorageClient) -> None:
        self.storage = storage


@pytest.fixture
def files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> DiagramFiles:
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
    storage = LocalStorageClient(config)
    layer = _StorageOnlyLayer(storage)

    monkeypatch.setattr(session_module, "current_user_id", lambda: "7")
    monkeypatch.setattr(session_module, "current_thread_id", lambda: THREAD)
    monkeypatch.setattr(DiagramFiles, "_layer", staticmethod(lambda: layer))

    return DiagramFiles(1000)


class TestSaveAndView:
    """Файл проходит цикл целиком: сохранение, чтение, показ вьювером."""

    @pytest.mark.anyio
    async def test_save_writes_normalized_file(
        self, files: DiagramFiles, tmp_path: Path
    ) -> None:
        key = await files.save("orders.mmd", f"```mermaid\n{ER_SPEC}\n```")

        assert key.in_workspace() == f"/workspace/{THREAD}/mermaid/orders.mmd"

        stored = tmp_path / "7" / THREAD / "mermaid" / "orders.mmd"
        assert stored.read_text(encoding="utf-8") == ER_SPEC

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

        assert opened.label == "orders.mmd"
        assert isinstance(opened.link, DiagramResult)
        assert opened.link.spec == ER_SPEC
        assert len(shown) == 1
        content = shown[0]
        assert content.path == key.in_workspace()
        assert content.text == ER_SPEC
        assert content.nonce

    @pytest.mark.anyio
    async def test_viewer_reads_user_upload(
        self, files: DiagramFiles, http_context: None, fast_verdict: None
    ) -> None:
        """Пользовательский .mmd из upload/ показывается тем же вьювером."""
        storage = files._layer().storage
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

        assert shown[0].text == ER_SPEC

    def test_viewer_handles_only_mmd(self, files: DiagramFiles) -> None:
        viewer = MermaidViewer(files)

        assert viewer.handles("orders.mmd") is True
        assert viewer.handles("report.pdf") is False

    @pytest.mark.anyio
    async def test_read_missing_file(self, files: DiagramFiles) -> None:
        key = ObjectKey.build(
            "7", THREAD, "no.mmd", "el-1", dir_thread=ThreadDir.MERMAID
        )

        with pytest.raises(DiagramRefusedError) as failure:
            await files.read(key)

        assert failure.value.kind == DiagramErrorKind.FILE_NOT_FOUND


class TestEntry:
    """Метаданные диаграммы: подпись и тип для панели и ленты."""

    FLOW_SPEC = "---\ntitle: Процесс\n---\nflowchart LR\n    A --> B"

    def test_entry_of_unparsed_spec_keeps_text(self) -> None:
        """Файл не mermaid: текст едет как есть, метаданных нет, подпись — имя."""
        key = ObjectKey.build(
            "7", THREAD, "a.mmd", "el-1", dir_thread=ThreadDir.MERMAID
        )

        entry = DiagramEntry.of(key, "не диаграмма вовсе")

        assert entry.spec == "не диаграмма вовсе"
        assert entry.type == ""
        assert entry.label == "a.mmd"

    def test_entry_of_broken_body_keeps_type(self) -> None:
        """Заголовок разобран — тип известен; синтаксис тела проверяет браузер."""
        key = ObjectKey.build(
            "7", THREAD, "a.mmd", "el-1", dir_thread=ThreadDir.MERMAID
        )

        entry = DiagramEntry.of(key, "erDiagram\n  A ||--")

        assert entry.spec == "erDiagram\n  A ||--"
        assert entry.type == "erDiagram"

    @pytest.mark.anyio
    async def test_read_binary_file(self, files: DiagramFiles) -> None:
        storage = files._layer().storage
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

        assert failure.value.kind == DiagramErrorKind.BAD_FILE

    @pytest.mark.anyio
    async def test_read_refuses_file_over_the_limit(self, files: DiagramFiles) -> None:
        """Потолок на объём держит тул: хранилище отдаёт что угодно потоком.

        Файл в mermaid/ пишет bash, поэтому он может быть сколь угодно велик,
        а спека целиком уезжает в props элемента и в LLM.
        """
        storage = files._layer().storage
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

        assert failure.value.kind == DiagramErrorKind.BAD_FILE


class FileFeed:
    """Источник содержимого для вотчера: лента снапшотов, управляемая тестом.

    Исчерпание ленты гасит alive — вотчер завершает себя, как при конце хода.
    """

    def __init__(self, initial: str, snapshots: list[str | None]) -> None:
        self._current = initial
        self._snapshots = snapshots
        self.pushed: list[str] = []
        self.alive = True

    async def read(self) -> str:
        if not self._snapshots:
            self.alive = False
            return self._current

        head = self._snapshots.pop(0)
        if head is None:
            raise DiagramRefusedError(
                DiagramErrorKind.FILE_NOT_FOUND, "файл переписывается"
            )

        self._current = head
        return head

    def is_alive(self) -> bool:
        return self.alive

    async def push(self, text: str) -> None:
        self.pushed.append(text)


class TestCanvasWatcher:
    """Слежение за файлом: обновления, пропуск битого тика, самоостановка."""

    @staticmethod
    async def run(feed: FileFeed) -> None:
        watcher = CanvasWatcher(
            read=feed.read,
            alive=feed.is_alive,
            push=feed.push,
            interval_sec=0.001,
        )
        await watcher.run(initial=feed._current)

    @pytest.mark.anyio
    async def test_pushes_only_changes(self) -> None:
        feed = FileFeed("v1", ["v1", "v1", "v2", "v3", "v3"])

        await self.run(feed)

        assert feed.pushed == ["v2", "v3"]

    @pytest.mark.anyio
    async def test_read_error_skips_tick(self) -> None:
        """Файл в момент чтения переписывается — тик пропущен, слежение живо."""
        feed = FileFeed("v1", ["v1", None, "v2"])

        await self.run(feed)

        assert feed.pushed == ["v2"]

    @pytest.mark.anyio
    async def test_stops_when_turn_ends(self) -> None:
        feed = FileFeed("v1", ["v2"])
        feed.alive = False

        await self.run(feed)

        assert feed.pushed == []

    @pytest.mark.anyio
    async def test_broken_snapshot_is_pushed_as_is(self) -> None:
        """Битая спека едет на канвас без фильтрации: ошибку показывает браузер."""
        feed = FileFeed("erDiagram\n  A ||--o{ B : x", ["erDiagram\n  A ||--"])

        await self.run(feed)

        assert feed.pushed == ["erDiagram\n  A ||--"]


class TestRenderVerdicts:
    """Вердикт браузера: отчёт находит ожидание по nonce, молчание — UNKNOWN."""

    @pytest.mark.anyio
    async def test_report_resolves_waiter(self) -> None:
        RenderVerdicts.expect("n-1")

        RenderVerdicts.report({"nonce": "n-1", "ok": False, "error": "Parse error"})
        verdict = await RenderVerdicts.wait("n-1", 1.0)

        assert verdict.status is RenderStatus.FAILED
        assert verdict.message == "Parse error"

    @pytest.mark.anyio
    async def test_success_report(self) -> None:
        RenderVerdicts.expect("n-2")

        RenderVerdicts.report({"nonce": "n-2", "ok": True, "error": ""})
        verdict = await RenderVerdicts.wait("n-2", 1.0)

        assert verdict.status is RenderStatus.RENDERED

    @pytest.mark.anyio
    async def test_silence_is_unknown(self) -> None:
        RenderVerdicts.expect("n-3")

        verdict = await RenderVerdicts.wait("n-3", 0.05)

        assert verdict.status is RenderStatus.UNKNOWN

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

        assert failure.value.kind == CanvasErrorKind.RENDER_FAILED
        assert "Parse error on line 5" in str(failure.value)


class FakeTurn:
    """Живой ход под тест: карточке нужен только id шага ответа."""

    answer_step_id = "answer-step"


class TestSaveToolEndToEnd:
    """diagram_save целиком: сохранить, карточку в ленту, вердикт — с панели.

    Во время хода смонтирована только панель, поэтому показ в ней и есть
    верификация спеки; карточка уходит без nonce и вердикт не репортит.
    """

    @pytest.fixture(autouse=True)
    def active_turn(self) -> Any:
        """Карточка цепляется к шагу ответа: без живого хода её некуда деть."""
        scope = TurnContext.open(THREAD, cast(Any, FakeTurn()))
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

    async def _call(
        self, spec: str, verdict: dict[str, Any], panel: list[Any]
    ) -> Any:
        """Зовёт тул как агент — tool_call, иначе artifact до вызывающего не дойдёт."""
        save = build_diagram_tools(DiagramToolConfig(max_chars=32000))[0]
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

        assert isinstance(result, ErrorResult)
        assert result.error_kind == CanvasErrorKind.RENDER_FAILED
        assert "Parse error on line 5" in result.message
        assert "diagram saved" in result.message

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

        assert feed == []

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

        assert isinstance(result, TextResult)
        assert "diagram saved" in content

        assert panel[0].kind == "mermaid"
        assert panel[0].text == ER_SPEC

        card = feed[0]
        assert card.props["kind"] == "mermaid"
        assert card.props["preview"] is True
        assert card.props["text"] == ER_SPEC
        assert not card.props.get("nonce"), "карточка не участвует в верификации"
        assert card.props["path"] == f"/workspace/{THREAD}/mermaid/orders.mmd"
