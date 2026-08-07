"""Каждый инструмент прогоняется по реальному конфигу (pytest -m integration).

Cgroup-лимиты сняты: pytest живёт вне делегированного cgroup (test_sandbox_cgroup).
"""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
from pathlib import Path
from typing import Any
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql

from boba.sandbox import (
    SandboxCaller,
    SandboxPayloadError,
    SandboxToolConfig,
)
from boba.settings import bind
from boba.tool.chart import build_chart_tools
from boba.tool.doc import DocToolsConfig, build_doc_tools
from boba.tool.kb import (
    PostgresKnowledgeBaseConfig,
    build_kb_tools,
)
from boba.tool.kb.confluence import (
    ConfluenceToolsConfig,
    build_confluence_tools,
)
from boba.tool.kb.confluence.ingest_base import ConfluenceIngestConfig
from boba.tool.kb.confluence.ingest_tools import (
    build_confluence_ingest_tools,
)
from boba.tool.kb.search import ConfluenceCollection
from boba.tool.pg import PgExecutorConfig, build_pg_tools
from boba.tool.shell import build_bash_tool
from boba.tool.web import WebGrepConfig, build_web_tools
from boba.toolkit.launcher import LauncherFactory, ToolLauncher
from boba.toolkit.result import (
    AffectedResult,
    ChartResult,
    ErrorResult,
    JsonResult,
    TableResult,
    TextResult,
    ToolArtifact,
)

_REPO = Path(__file__).resolve().parents[4]
_ROOTFS = _REPO / "build" / "src" / "sandbox" / "rootfs"

_UID = os.getuid()
_DELEGATED = f"/sys/fs/cgroup/user.slice/user-{_UID}.slice/user@{_UID}.service"


def _inside_delegated_scope() -> bool:
    """Мигрировать в cgroup_base можно только из его же поддерева."""
    with open("/proc/self/cgroup") as f:
        current = f.read().strip().split(":")[-1]
    return current.startswith(f"/user.slice/user-{_UID}.slice/user@{_UID}.service")


pytestmark = [
    pytest.mark.integration,
    pytest.mark.anyio,
    pytest.mark.skipif(
        shutil.which("bwrap") is None or not (_ROOTFS / "bin" / "sh").exists(),
        reason="нет bwrap или артефактов песочницы (собрать: make deps)",
    ),
    pytest.mark.skipif(
        not os.path.isdir(_DELEGATED) or not _inside_delegated_scope(),
        reason=(
            "pytest вне делегированного scope: cgroup-лимиты профиля не "
            "применятся. Запуск: systemd-run --user --scope --slice="
            "boba-sandbox.slice pytest ... (или конфигурация из launch.json)"
        ),
    ),
]

USER_ID = "integration"
THREAD_ID = "t-integration"

# Двухстраничный PDF: стр.1 "Alpha page one", стр.2 "Beta page two Alpha again".
SAMPLE_PDF = b"""%PDF-1.4
1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj
2 0 obj<</Type/Pages/Kids[3 0 R 6 0 R]/Count 2>>endobj
3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 300 300]/Contents 4 0 R\
/Resources<</Font<</F1 5 0 R>>>>>>endobj
4 0 obj<</Length 50>>stream
BT /F1 20 Tf 20 200 Td (Alpha page one) Tj ET
endstream endobj
5 0 obj<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>endobj
6 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 300 300]/Contents 7 0 R\
/Resources<</Font<</F1 5 0 R>>>>>>endobj
7 0 obj<</Length 60>>stream
BT /F1 20 Tf 20 200 Td (Beta page two Alpha again) Tj ET
endstream endobj
trailer<</Root 1 0 R/Size 8>>
%%EOF"""

WORKSPACE_PDF = "/workspace/integration.pdf"


class ToolSetup:
    """Сборка инструмента из конфига приложения для прогона вне chainlit."""

    @staticmethod
    def config(raw: Any, section: str, model: type) -> Any:
        """Секция конфига как есть, с cgroup-лимитами — они часть контракта."""
        return bind(raw, path=section, model=model)

    @staticmethod
    def path_vars() -> dict[str, str]:
        return {"user_id": USER_ID, "thread_id": THREAD_ID}

    @staticmethod
    def launchers(raw: Any, section: str) -> LauncherFactory:
        """Исполнитель на профиле секции: окружение выбирает вызывающий."""
        sandbox = bind(raw, path=f"{section}.sandbox", model=SandboxToolConfig)
        profile = sandbox.effective()

        def launcher(tool: str) -> ToolLauncher:
            return SandboxCaller(tool, profile, ToolSetup.path_vars)

        return launcher

    @staticmethod
    def by_name(built: list[Any]) -> dict[str, Any]:
        tools: dict[str, Any] = {}
        for tool in built:
            tools[tool.name] = tool
        return tools


class Call:
    """Вызов инструмента: ответ разбирается как типизированный artifact."""

    @staticmethod
    async def result(tool: Any, **args: Any) -> Any:
        message = await tool.ainvoke(
            {"name": tool.name, "args": args, "id": "c1", "type": "tool_call"}
        )
        result = ToolArtifact.revive(message.artifact)
        assert result is not None, f"{tool.name}: artifact не разобран"
        return result

    @staticmethod
    async def ok(tool: Any, **args: Any) -> Any:
        result = await Call.result(tool, **args)
        assert result.ok, f"{tool.name}: {result}"
        return result


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    pass


@pytest.fixture(scope="module")
def bash_tool(raw_config):
    return build_bash_tool(ToolSetup.launchers(raw_config, "tool.bash"))


@pytest.fixture(scope="module")
def doc_tools(raw_config):
    cfg = ToolSetup.config(raw_config, "tool.doc", DocToolsConfig)
    launchers = ToolSetup.launchers(raw_config, "tool.doc")
    return ToolSetup.by_name(build_doc_tools(cfg, launchers))


@pytest.fixture(scope="module")
def chart_tool(raw_config):
    launchers = ToolSetup.launchers(raw_config, "tool.chart")
    return ToolSetup.by_name(build_chart_tools(launchers))["visualize"]


@pytest.fixture(scope="module")
def web_tools(raw_config):
    cfg = ToolSetup.config(raw_config, "tool.web", WebGrepConfig)
    launchers = ToolSetup.launchers(raw_config, "tool.web")
    return ToolSetup.by_name(build_web_tools(cfg, launchers))


@pytest.fixture(scope="module")
def whitelisted_url(raw_config) -> str:
    """Адрес для web-тестов берётся из whitelist'а: другие хосты запрещены."""
    cfg = bind(raw_config, path="tool.web", model=WebGrepConfig)
    hosts = sorted(cfg.profiles)
    assert hosts, "[tool.web.profiles] пуст — web-инструментам некуда ходить"
    return f"https://{hosts[0]}/"


@pytest.fixture(scope="module")
def confluence_tools(raw_config):
    cfg = ToolSetup.config(raw_config, "tool.confluence", ConfluenceToolsConfig)
    launchers = ToolSetup.launchers(raw_config, "tool.confluence")
    return ToolSetup.by_name(build_confluence_tools(cfg, launchers))


@pytest.fixture(scope="module")
def pg_tools(raw_config):
    cfg = ToolSetup.config(raw_config, "tool.pg", PgExecutorConfig)
    launchers = ToolSetup.launchers(raw_config, "tool.pg")
    return ToolSetup.by_name(build_pg_tools(cfg, launchers))


@pytest.fixture(scope="module")
async def kb_collection(raw_config):
    """Своя коллекция на прогон: рабочая kb_confluence остаётся нетронутой."""
    cfg = bind(raw_config, path="tool.ingest", model=ConfluenceIngestConfig)
    name = f"kb_it_{uuid4().hex[:8]}"
    previous = ConfluenceCollection.COLLECTION
    ConfluenceCollection.COLLECTION = name
    try:
        yield name
    finally:
        ConfluenceCollection.COLLECTION = previous
        await KbCleanup.drop(cfg, name)


class KbCleanup:
    """Уборка тестовой коллекции: чанки и запись в реестре коллекций."""

    @staticmethod
    async def drop(cfg: ConfluenceIngestConfig, collection: str) -> None:
        statements = (
            sql.SQL("delete from {} where collection = %s").format(
                sql.Identifier(cfg.tables.pg_schema, cfg.tables.chunks_table)
            ),
            sql.SQL("delete from {} where name = %s").format(
                sql.Identifier(cfg.tables.pg_schema, cfg.tables.collections_table)
            ),
        )
        conn = await psycopg.AsyncConnection.connect(**cfg.connection.conn_settings())
        async with conn:
            for statement in statements:
                await conn.execute(statement, (collection,))
            await conn.commit()


@pytest.fixture(scope="module")
def ingest_tools(raw_config, kb_collection: str):
    cfg = ToolSetup.config(raw_config, "tool.ingest", ConfluenceIngestConfig)
    cfg = cfg.model_copy(update={"collection": kb_collection})
    launchers = ToolSetup.launchers(raw_config, "tool.ingest")
    return ToolSetup.by_name(build_confluence_ingest_tools(cfg, launchers))


@pytest.fixture(scope="module")
def kb_tools(raw_config, kb_collection: str):
    cfg = ToolSetup.config(raw_config, "tool.kb", PostgresKnowledgeBaseConfig)
    launchers = ToolSetup.launchers(raw_config, "tool.kb")
    return ToolSetup.by_name(build_kb_tools(cfg, launchers))


@pytest.fixture(scope="module")
def workspace_image(raw_config):
    """Образ тестового пользователя: создаётся из шаблона и сносится после."""
    sandbox = ToolSetup.config(raw_config, "tool.bash.sandbox", SandboxToolConfig)
    profile = sandbox.effective().render(ToolSetup.path_vars())
    image = Path(profile.rw_images[0].host)
    yield image
    for path in (image, Path(f"{image}.lock")):
        path.unlink(missing_ok=True)
    shutil.rmtree(f"{image}.mnt", ignore_errors=True)


@pytest.fixture(scope="module")
async def workspace_pdf(bash_tool, workspace_image) -> str:
    """PDF кладётся в образ тем же путём, каким его туда положит пользователь."""
    payload = base64.b64encode(SAMPLE_PDF).decode()
    result = await Call.ok(
        bash_tool,
        command=f"base64 -d > {WORKSPACE_PDF}; test -s {WORKSPACE_PDF}",
        stdin=payload,
    )
    assert result.payload["exit_code"] == 0
    return WORKSPACE_PDF


@pytest.fixture(scope="module")
async def confluence_page(confluence_tools) -> dict[str, str]:
    """Страница берётся из живого поиска: жёсткие id ломаются со стендом."""
    spaces = await Call.ok(confluence_tools["confluence_spaces"], limit=10)
    assert spaces.rows, "в Confluence нет ни одного space"
    found = await Call.ok(
        confluence_tools["confluence_search"],
        query="данные",
        limit=10,
        snippet_chars=200,
    )
    for row in found.rows:
        if row["title"].count(".") == 0:
            return {
                "page_id": row["page_id"],
                "title": row["title"],
                "space_key": row["space_key"],
            }
    pytest.skip("поиск не вернул ни одной страницы (только вложения)")


@pytest.fixture(scope="module")
async def confluence_attachment_ref(confluence_tools) -> dict[str, str]:
    """Вложение ищется по расширению; page_id страницы лежит в ссылке."""
    found = await Call.ok(
        confluence_tools["confluence_search"],
        query="docx",
        limit=20,
        snippet_chars=100,
    )
    for row in found.rows:
        if not row["title"].endswith(".docx"):
            continue
        match = re.search(r"pageId=(\d+)", row["url"])
        if match is None:
            continue
        return {"page_id": match.group(1), "filename": row["title"]}
    pytest.skip("на стенде не нашлось .docx-вложения")


class TestBashTool:
    """bash: команда идёт в песочницу, рабочая папка — образ пользователя."""

    async def test_command_runs(self, bash_tool, workspace_image) -> None:
        result = await Call.ok(bash_tool, command="echo hello; pwd")
        assert isinstance(result, JsonResult)
        assert result.payload["exit_code"] == 0
        assert "hello" in result.payload["stdout"]
        assert "/workspace" in result.payload["stdout"]

    async def test_stdin_reaches_command(self, bash_tool, workspace_image) -> None:
        result = await Call.ok(bash_tool, command="cat", stdin="через stdin")
        assert result.payload["stdout"] == "через stdin"

    async def test_failed_command_is_not_ok(self, bash_tool, workspace_image) -> None:
        result = await Call.result(bash_tool, command="echo boom >&2; exit 3")
        assert not result.ok
        assert result.payload["exit_code"] == 3
        assert "boom" in result.payload["stderr"]

    async def test_network_is_unavailable(self, bash_tool, workspace_image) -> None:
        """Профиль bash без сети: имена не резолвятся, наружу хода нет."""
        result = await Call.result(bash_tool, command="getent hosts confl.loshara.com")
        assert not result.ok


class TestDocTools:
    """doc: liteparse читает документ из образа пользователя."""

    async def test_read_document_all_pages(self, doc_tools, workspace_pdf) -> None:
        result = await Call.ok(
            doc_tools["read_document"],
            path=workspace_pdf,
            pages="1-2",
            ocr_enabled=False,
            num_workers=1,
            ocr_language="rus+eng",
        )
        assert isinstance(result, TextResult)
        assert "Alpha page one" in result.text
        assert "Beta page two" in result.text
        assert result.metadata["pages"] == "1,2"

    async def test_document_outline(self, doc_tools, workspace_pdf) -> None:
        result = await Call.ok(
            doc_tools["document_outline"],
            path=workspace_pdf,
            ocr_enabled=False,
            num_workers=1,
            ocr_language="rus+eng",
        )
        assert isinstance(result, TableResult)
        pages = []
        for row in result.rows:
            pages.append(row["page"])
        assert pages == [1, 2]

    async def test_read_document_pages_subset(self, doc_tools, workspace_pdf) -> None:
        result = await Call.ok(
            doc_tools["read_document"],
            path=workspace_pdf,
            pages="2",
            ocr_enabled=False,
            num_workers=1,
            ocr_language="rus+eng",
        )
        assert "Beta page two" in result.text
        assert "Alpha page one" not in result.text

    async def test_search_document(self, doc_tools, workspace_pdf) -> None:
        result = await Call.ok(
            doc_tools["search_document"],
            path=workspace_pdf,
            query="Alpha",
            ocr_enabled=False,
            num_workers=1,
            ocr_language="rus+eng",
        )
        assert isinstance(result, TableResult)
        assert len(result.rows) == 2
        assert result.rows[0]["page"] == 1

    async def test_missing_document_fails_loudly(self, doc_tools) -> None:
        with pytest.raises(SandboxPayloadError):
            await Call.result(
                doc_tools["read_document"],
                path="/workspace/no.pdf",
                pages="1",
                ocr_enabled=False,
                num_workers=1,
                ocr_language="rus+eng",
            )


class TestChartTool:
    """chart: спеку проверяет payload, приложение plotly не держит."""

    async def test_valid_figure(self, chart_tool) -> None:
        spec = json.dumps(
            {
                "data": [{"type": "bar", "x": ["a", "b"], "y": [1, 2]}],
                "layout": {"title": "итоги"},
            }
        )
        result = await Call.ok(chart_tool, spec=spec)
        assert isinstance(result, ChartResult)
        assert result.title == "итоги"
        assert result.spec["data"][0]["type"] == "bar"

    async def test_broken_spec_fails_loudly(self, chart_tool) -> None:
        with pytest.raises(SandboxPayloadError):
            await Call.result(chart_tool, spec="не json")


class TestWebTools:
    """web: HTTP-запрос и разбор HTML идут внутри песочницы."""

    async def test_fetch_page(self, web_tools, whitelisted_url) -> None:
        result = await Call.ok(
            web_tools["web_fetch_page"],
            url=whitelisted_url,
            as_markdown=True,
            line_offset=0,
            line_count=20,
        )
        assert result.payload["total_lines"] > 0
        assert result.payload["returned_lines"] <= 20
        assert result.payload["source_url"] == whitelisted_url

    async def test_grep_page(self, web_tools, whitelisted_url) -> None:
        result = await Call.ok(
            web_tools["web_grep_page"],
            url=whitelisted_url,
            pattern="Confluence",
            limit=3,
        )
        assert isinstance(result, TableResult)
        assert result.rows
        assert "Confluence" in result.rows[0]["content"]

    async def test_host_outside_whitelist(self, web_tools) -> None:
        result = await Call.result(
            web_tools["web_fetch_page"],
            url="https://example.com/",
            as_markdown=True,
            line_offset=0,
            line_count=5,
        )
        assert isinstance(result, ErrorResult)
        assert "whitelist" in result.message


class TestConfluenceTools:
    """confluence: REST-запрос и разбор ответа — целиком в песочнице."""

    async def test_spaces(self, confluence_tools) -> None:
        result = await Call.ok(confluence_tools["confluence_spaces"], limit=10)
        assert isinstance(result, TableResult)
        assert result.rows
        assert set(result.rows[0]) >= {"key", "name", "type"}

    async def test_search(self, confluence_tools) -> None:
        result = await Call.ok(
            confluence_tools["confluence_search"],
            query="данные",
            limit=5,
            snippet_chars=200,
        )
        assert result.rows
        assert set(result.rows[0]) >= {"page_id", "title", "space_key", "url"}

    async def test_fetch_page(self, confluence_tools, confluence_page) -> None:
        result = await Call.ok(
            confluence_tools["confluence_fetch"],
            page_id=confluence_page["page_id"],
            as_markdown=True,
        )
        assert isinstance(result, TextResult)
        assert result.text.strip()

    async def test_grep_page(self, confluence_tools, confluence_page) -> None:
        word = confluence_page["title"].split()[0]
        result = await Call.ok(
            confluence_tools["confluence_grep"],
            page_id=confluence_page["page_id"],
            pattern=word,
            case_insensitive=True,
            limit=3,
        )
        assert isinstance(result, TableResult)

    async def test_unknown_page_reports_error(self, confluence_tools) -> None:
        result = await Call.result(
            confluence_tools["confluence_fetch"], page_id="0", as_markdown=True
        )
        assert isinstance(result, ErrorResult)


class TestPgTools:
    """pg: соединение, kerberos и SQL исполняются внутри песочницы."""

    async def test_list_targets(self, pg_tools) -> None:
        result = await Call.ok(pg_tools["pg_list_targets"])
        targets = []
        for row in result.rows:
            targets.append(row["connection_name"])
        assert targets

    async def test_list_tables(self, pg_tools) -> None:
        result = await Call.ok(
            pg_tools["pg_list_tables"],
            connection_name="main",
            pg_schema="pg_catalog",
        )
        assert result.rows
        assert set(result.rows[0]) >= {"schema", "table_name", "kind", "owner"}

    async def test_system_schemas_are_not_hidden(self, pg_tools) -> None:
        """Каталог не прячется: системные схемы видны наравне с остальными."""
        result = await Call.ok(pg_tools["pg_list_tables"], connection_name="main")
        schemas = set()
        for row in result.rows:
            schemas.add(row["schema"])
        assert schemas

    async def test_table_pattern_filters_by_name(self, pg_tools) -> None:
        result = await Call.ok(
            pg_tools["pg_list_tables"],
            connection_name="main",
            pg_schema="pg_catalog",
            table_pattern="pg_cl%",
        )
        assert result.rows
        for row in result.rows:
            assert row["table_name"].startswith("pg_cl")

    async def test_describe_table(self, pg_tools) -> None:
        tables = await Call.ok(
            pg_tools["pg_list_tables"],
            connection_name="main",
            pg_schema="pg_catalog",
            table_pattern="pg_class",
        )
        first = tables.rows[0]
        result = await Call.ok(
            pg_tools["pg_describe_table"],
            connection_name="main",
            table=first["table_name"],
            pg_schema=first["schema"],
        )
        assert result.rows
        assert set(result.rows[0]) >= {"column_name", "type", "nullable", "primary_key"}

    async def test_query_returns_rows(self, pg_tools) -> None:
        result = await Call.ok(
            pg_tools["pg_query"],
            connection_name="main",
            sql="select 1 as one, 'два' as two",
        )
        assert isinstance(result, TableResult)
        assert result.rows[0]["one"] == 1
        assert result.rows[0]["two"] == "два"

    async def test_statement_without_rows_reports_status(self, pg_tools) -> None:
        """DDL проходит и отчитывается статусом; временная таблица живёт в сессии."""
        result = await Call.ok(
            pg_tools["pg_query"],
            connection_name="main",
            sql="create temp table integration_probe(x int)",
        )
        assert isinstance(result, AffectedResult)
        assert result.status == "CREATE TABLE"

    async def test_export_returns_copy_text(self, pg_tools) -> None:
        result = await Call.ok(
            pg_tools["pg_export"],
            connection_name="main",
            sql="select 1 as one, 'два' as two",
        )
        assert "one" in result.text
        assert "два" in result.text

    async def test_unknown_target_is_rejected(self, pg_tools) -> None:
        result = await Call.result(
            pg_tools["pg_query"], connection_name="нет-такого", sql="select 1"
        )
        assert isinstance(result, ErrorResult)


class TestIngestTools:
    """ingest: обход Confluence, чтение вложений и запись в KB — в песочнице."""

    async def test_index_pages(
        self, ingest_tools, confluence_page, kb_collection
    ) -> None:
        result = await Call.ok(
            ingest_tools["confluence_index_pages"],
            page_ids=[confluence_page["page_id"]],
            prune_missing=False,
            force_update=True,
        )
        stats = result.rows[0]
        assert stats["collection"] == kb_collection
        assert stats["indexed"] > 0
        assert stats["failed"] == 0

    async def test_index_cql_skips_unchanged(
        self, ingest_tools, confluence_page
    ) -> None:
        """Повтор по той же странице — та же выгрузка, переиндексации нет."""
        result = await Call.ok(
            ingest_tools["confluence_index_cql"],
            cql=f"id = {confluence_page['page_id']}",
            prune_missing=False,
        )
        stats = result.rows[0]
        assert stats["skipped_unchanged"] == 1
        assert stats["indexed"] == 0

    async def test_index_spaces(
        self, ingest_tools, confluence_page, kb_collection
    ) -> None:
        """Обход целого space'а: страницы уже в коллекции — переиндексации нет."""
        result = await Call.ok(
            ingest_tools["confluence_index_spaces"],
            space_keys=[confluence_page["space_key"]],
            prune_missing=False,
            force_update=False,
        )
        stats = result.rows[0]
        assert stats["collection"] == kb_collection
        assert stats["failed"] == 0
        assert stats["skipped_unchanged"] >= 1

    async def test_unknown_space_reports_error(self, ingest_tools) -> None:
        result = await Call.result(
            ingest_tools["confluence_index_spaces"],
            space_keys=["NOSUCHSPACE"],
            prune_missing=False,
            force_update=False,
        )
        assert isinstance(result, ErrorResult)

    async def test_fetch_attachment(
        self, ingest_tools, confluence_attachment_ref
    ) -> None:
        result = await Call.ok(
            ingest_tools["confluence_attachment"],
            page_id=confluence_attachment_ref["page_id"],
            filename=confluence_attachment_ref["filename"],
        )
        assert isinstance(result, TextResult)
        assert result.text.strip()


class TestKbTools:
    """kb: эмбеддинг и SQL идут в песочнице, ищут по свежему индексу."""

    async def test_fts_search_finds_indexed_page(
        self, kb_tools, ingest_tools, confluence_page
    ) -> None:
        await Call.ok(
            ingest_tools["confluence_index_pages"],
            page_ids=[confluence_page["page_id"]],
            prune_missing=False,
            force_update=False,
        )
        result = await Call.ok(
            kb_tools["kb_fts_search"], query=confluence_page["title"], top_k=20
        )
        assert isinstance(result, TableResult)
        assert result.rows
        found = []
        for row in result.rows:
            found.append(row["page_id"])
        assert confluence_page["page_id"] in found

    async def test_vector_search_returns_hits(
        self, kb_tools, ingest_tools, confluence_page
    ) -> None:
        await Call.ok(
            ingest_tools["confluence_index_pages"],
            page_ids=[confluence_page["page_id"]],
            prune_missing=False,
            force_update=False,
        )
        result = await Call.ok(
            kb_tools["kb_vector_search"], query=confluence_page["title"], top_k=5
        )
        assert result.rows
        assert set(result.rows[0]) >= {"id", "distance", "snippet"}
