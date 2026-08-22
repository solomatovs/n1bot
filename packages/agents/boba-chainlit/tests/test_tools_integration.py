"""Каждый инструмент прогоняется по реальному конфигу (pytest -m integration).

Cgroup-лимиты сняты: pytest живёт вне делегированного cgroup (test_sandbox_cgroup).
"""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from psycopg import sql

from boba.chainlit.agent.toolrun.injected import InjectedConfig
from boba.chainlit.infra.plugins import as_structured_tool, warmup_configs
from boba.chainlit.rendering.tool import ToolCallMarkdown, ToolResultMarkdown
from boba.db.postgres import AsyncPostgresPool
from boba.sandbox import (
    SandboxToolConfig,
)
from boba.sandbox.zygote import ZygotePolicy, ZygoteRegistry, ZygoteToolCaller
from boba.settings import bind
from boba.tool.kb.confluence.ingest_base import ConfluenceIngestConfig
from boba.tool.kb.search import ConfluenceCollection
from boba.tool.shell.tools import BashToolConfig, build_bash_tool
from boba.tool.web.tools import WebGrepConfig
from boba.toolkit.calls import ScriptCall
from boba.toolkit.entry import ToolMain
from boba.toolkit.launcher import LauncherFactory, PayloadFailureError, ToolLauncher
from boba.toolkit.result import (
    AffectedSqlResult,
    ChartResult,
    MultiResult,
    ShellResult,
    TableResult,
    TextResult,
    ToolArtifact,
)
from boba.toolkit.wrap import ToolProcessWrap

_REPO = Path(__file__).resolve().parents[4]
_ROOTFS = _REPO / "build" / "src" / "sandbox" / "rootfs"

_CGROUP_BASE = os.environ.get("BOBA_CGROUP_BASE", "/sys/fs/cgroup/boba")


def _cgroup_delegated() -> bool:
    """Миграция в cgroup_base из session-scope: нужна запись в саму базу и в
    cgroup.procs общего предка (корня cgroup) — это готовит boba-cgroup.service."""
    base_ok = os.access(os.path.join(_CGROUP_BASE, "cgroup.procs"), os.W_OK)
    root_ok = os.access("/sys/fs/cgroup/cgroup.procs", os.W_OK)
    return base_ok and root_ok


pytestmark = [
    pytest.mark.integration,
    pytest.mark.anyio,
    pytest.mark.skipif(
        shutil.which("bwrap") is None or not (_ROOTFS / "bin" / "sh").exists(),
        reason="нет bwrap или артефактов песочницы (собрать: make deps)",
    ),
    pytest.mark.skipif(
        not _cgroup_delegated(),
        reason=(
            f"cgroup base {_CGROUP_BASE} не делегирован пользователю: "
            "cgroup-лимиты профиля не применятся "
            "(systemctl enable --now boba-cgroup.service)"
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


ZYGOTE = ZygotePolicy(
    start_timeout_sec=60.0,
    max_start_attempts=1,
    restart_backoff_sec=0.05,
    healthy_after_sec=0.5,
    stop_wait_sec=5.0,
    call_poll_sec=0.05,
)


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
    def caller(raw: Any, section: str, modules: Sequence[str] = ()) -> ZygoteToolCaller:
        """Зигота секции конфига: тот же путь запуска, что в приложении."""
        sandbox = bind(raw, path=f"tool.{section}.sandbox", model=SandboxToolConfig)
        profile = sandbox.profile

        supervisor = ZygoteRegistry.obtain(
            section,
            profile,
            modules,
            ZYGOTE,
            warmup_calls=warmup_configs(section, modules, raw),
        )
        return ZygoteToolCaller(section, supervisor, profile, ToolSetup.path_vars)

    @staticmethod
    def launchers(raw: Any, section: str) -> LauncherFactory:
        """Фабрика исполнителей секции: одна зигота на все её инструменты."""
        caller = ToolSetup.caller(raw, section)

        def launcher(tool: str) -> ToolLauncher:
            return caller

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
        if result is None:
            raise AssertionError(f"{tool.name}: artifact не разобран")
        return result

    @staticmethod
    async def ok(tool: Any, **args: Any) -> Any:
        result = await Call.result(tool, **args)
        if not (result.ok):
            raise AssertionError(f"{tool.name}: {result}")
        return result


@pytest.fixture(scope="module", autouse=True)
def stop_zygotes():
    """Зиготы секций гасятся после модуля, как это делает выход приложения."""
    yield
    ZygoteRegistry.stop_all()


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    pass


@pytest.fixture(scope="module")
def bash_tool(raw_config):
    cfg = ToolSetup.config(raw_config, "tool.bash", BashToolConfig)
    launchers = ToolSetup.launchers(raw_config, "bash")

    return as_structured_tool(build_bash_tool(cfg, launchers))


@pytest.fixture(scope="module")
def doc_tools(raw_config):
    """doc-функции новой модели: обёртка запуска + конфиг, как в загрузчике."""
    from importlib import reload

    import boba.tool.doc.tools as doc_module

    module = reload(doc_module)

    launcher = ToolSetup.caller(raw_config, "doc", [module.__name__])

    functions = [as_structured_tool(tool) for tool in module.TOOLS]
    ToolProcessWrap.guard_all(ToolMain.toolset(*functions), launcher)

    def resolve(name: str, annotation: Any) -> object:
        return bind(raw_config, path=annotation.SECTION, model=annotation)

    InjectedConfig.bind_all(functions, resolve)

    return ToolSetup.by_name(functions)


@pytest.fixture(scope="module")
def chart_tool(raw_config):
    """visualize новой модели: обёртка запуска на профиле секции."""
    from importlib import reload

    import boba.tool.chart.tools as chart_module

    module = reload(chart_module)

    launcher = ToolSetup.caller(raw_config, "chart", [module.__name__])

    visualize = as_structured_tool(module.visualize)
    ToolProcessWrap.guard_all(ToolMain.toolset(visualize), launcher)
    return visualize


@pytest.fixture(scope="module")
def web_tools(raw_config):
    """web-функции новой модели: обёртка запуска + конфиг, как в загрузчике."""
    from importlib import reload

    import boba.tool.web.tools as web_module

    module = reload(web_module)

    launcher = ToolSetup.caller(raw_config, "web", [module.__name__])

    functions = [as_structured_tool(tool) for tool in module.TOOLS]
    ToolProcessWrap.guard_all(ToolMain.toolset(*functions), launcher)

    def resolve(name: str, annotation: Any) -> object:
        return bind(raw_config, path=annotation.SECTION, model=annotation)

    InjectedConfig.bind_all(functions, resolve)

    return ToolSetup.by_name(functions)


@pytest.fixture(scope="module")
def whitelisted_url(raw_config) -> str:
    """Адрес для web-тестов берётся из whitelist'а: другие хосты запрещены."""
    cfg = bind(raw_config, path="tool.web", model=WebGrepConfig)
    hosts = sorted(cfg.profiles)
    if not (hosts):
        raise AssertionError(
            "[tool.web.profiles] пуст — web-инструментам некуда ходить"
        )
    return f"https://{hosts[0]}/"


@pytest.fixture(scope="module")
def confluence_tools(raw_config):
    """confluence-функции новой модели: обёртка запуска + конфиг."""
    from importlib import reload

    import boba.tool.kb.confluence.tools as confluence_module

    module = reload(confluence_module)

    launcher = ToolSetup.caller(raw_config, "confluence", [module.__name__])

    functions = [as_structured_tool(tool) for tool in module.TOOLS]
    ToolProcessWrap.guard_all(ToolMain.toolset(*functions), launcher)

    def resolve(name: str, annotation: Any) -> object:
        return bind(raw_config, path=annotation.SECTION, model=annotation)

    InjectedConfig.bind_all(functions, resolve)

    return ToolSetup.by_name(functions)


@pytest.fixture(scope="module")
def pg_tools(raw_config):
    """pg-функции новой модели: обёртка запуска + конфиг, как в загрузчике."""
    from importlib import reload

    import boba.tool.pg.tools as pg_module

    module = reload(pg_module)

    launcher = ToolSetup.caller(raw_config, "pg", [module.__name__])

    functions = [as_structured_tool(tool) for tool in module.TOOLS]
    ToolProcessWrap.guard_all(ToolMain.toolset(*functions), launcher)

    def resolve(name: str, annotation: Any) -> object:
        return bind(raw_config, path=annotation.SECTION, model=annotation)

    InjectedConfig.bind_all(functions, resolve)

    return ToolSetup.by_name(functions)


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
        """Подключение — через пул приложения: kerberos-ccache из keytab."""
        statements = (
            sql.SQL(
                """
                delete from
                    {}
                where
                    collection = %s
                """
            ).format(sql.Identifier(cfg.tables.pg_schema, cfg.tables.chunks_table)),
            sql.SQL(
                """
                delete from
                    {}
                where
                    name = %s
                """
            ).format(
                sql.Identifier(cfg.tables.pg_schema, cfg.tables.collections_table)
            ),
        )

        pool = AsyncPostgresPool(cfg.connection)
        await pool.open()
        try:
            async with pool.connection() as conn, conn.transaction():
                for statement in statements:
                    await conn.execute(statement, (collection,))
        finally:
            await pool.close()


@pytest.fixture(scope="module")
def ingest_tools(raw_config, kb_collection: str):
    """ingest-функции новой модели: обёртка запуска + конфиг прогона."""
    from importlib import reload

    import boba.tool.kb.confluence.ingest_tools as ingest_module

    module = reload(ingest_module)

    launcher = ToolSetup.caller(raw_config, "ingest", [module.__name__])

    functions = [as_structured_tool(tool) for tool in module.TOOLS]
    ToolProcessWrap.guard_all(ToolMain.toolset(*functions), launcher)

    def resolve(name: str, annotation: Any) -> object:
        cfg = bind(raw_config, path=annotation.SECTION, model=annotation)
        return cfg.model_copy(update={"collection": kb_collection})

    InjectedConfig.bind_all(functions, resolve)

    return ToolSetup.by_name(functions)


@pytest.fixture(scope="module")
def kb_tools(raw_config, kb_collection: str):
    """kb-функции новой модели: обёртка запуска + конфиг, как в загрузчике."""
    from importlib import reload

    import boba.tool.kb.tools as kb_module

    module = reload(kb_module)

    launcher = ToolSetup.caller(raw_config, "kb", [module.__name__])

    functions = [as_structured_tool(tool) for tool in module.TOOLS]
    ToolProcessWrap.guard_all(ToolMain.toolset(*functions), launcher)

    def resolve(name: str, annotation: Any) -> object:
        cfg = bind(raw_config, path=annotation.SECTION, model=annotation)
        return cfg.model_copy(update={"collection": kb_collection})

    InjectedConfig.bind_all(functions, resolve)

    return ToolSetup.by_name(functions)


@pytest.fixture(scope="module")
def workspace_image(raw_config):
    """Образ тестового пользователя: создаётся из шаблона и сносится после."""
    sandbox = ToolSetup.config(raw_config, "tool.bash.sandbox", SandboxToolConfig)
    profile = sandbox.profile.render(ToolSetup.path_vars())
    image = Path(profile.mounts.images[0].host)
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
    if result.exit_code != 0:
        raise AssertionError("result.exit_code == 0")
    return WORKSPACE_PDF


@pytest.fixture(scope="module")
async def confluence_page(confluence_tools) -> dict[str, str]:
    """Страница берётся из живого поиска: жёсткие id ломаются со стендом."""
    spaces = await Call.ok(confluence_tools["confluence_spaces"], limit=10)
    if not (spaces.rows):
        raise AssertionError("в Confluence нет ни одного space")
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
        if not (isinstance(result, ShellResult)):
            raise AssertionError("isinstance(result, ShellResult)")
        if result.exit_code != 0:
            raise AssertionError("result.exit_code == 0")
        if "hello" not in result.stdout:
            raise AssertionError('"hello" in result.stdout')
        if "/workspace" not in result.stdout:
            raise AssertionError('"/workspace" in result.stdout')

    async def test_call_and_result_render_as_script_and_exit_code(
        self, bash_tool, workspace_image
    ) -> None:
        """Показ вызова: команда — bash-блок входа, вывод — блок с кодом."""
        result = await Call.ok(bash_tool, command="echo hello")

        rendering = ToolCallMarkdown(
            ScriptCall(arg="command", lang="bash"),
            {"command": "echo hello", "stdin": ""},
        ).render()
        md = ToolResultMarkdown(result).render()

        if rendering is None:
            raise AssertionError("rendering is not None")
        if rendering.markdown != "```bash\necho hello\n```":
            raise AssertionError('rendering.markdown == "```bash\\necho hello\\n```"')
        if "```stdout\nhello\n```" not in md:
            raise AssertionError('"```stdout\\nhello\\n```" in md')
        if "_exit code: 0_" not in md:
            raise AssertionError('"_exit code: 0_" in md')

    async def test_stdin_reaches_command(self, bash_tool, workspace_image) -> None:
        result = await Call.ok(bash_tool, command="cat", stdin="через stdin")
        if result.stdout != "через stdin":
            raise AssertionError('result.stdout == "через stdin"')

    async def test_failed_command_is_not_ok(self, bash_tool, workspace_image) -> None:
        result = await Call.result(bash_tool, command="echo boom >&2; exit 3")
        if result.ok:
            raise AssertionError("not result.ok")
        if result.exit_code != 3:
            raise AssertionError("result.exit_code == 3")
        if "boom" not in result.stderr:
            raise AssertionError('"boom" in result.stderr')

    async def test_silent_failure_shows_stderr(
        self, bash_tool, workspace_image
    ) -> None:
        """Команда молчит в stdout: на экран идёт stderr, а не пустой блок."""
        result = await Call.result(bash_tool, command="echo boom >&2; exit 3")

        md = ToolResultMarkdown(result).render()

        if result.output.strip() != "boom":
            raise AssertionError('result.output.strip() == "boom"')
        if "```stderr\nboom\n```" not in md:
            raise AssertionError('"```stderr\\nboom\\n```" in md')
        if "_exit code: 3_" not in md:
            raise AssertionError('"_exit code: 3_" in md')

    async def test_network_is_unavailable(self, bash_tool, workspace_image) -> None:
        """Профиль bash без сети: имена не резолвятся, наружу хода нет."""
        result = await Call.result(bash_tool, command="getent hosts confl.loshara.com")
        if result.ok:
            raise AssertionError("not result.ok")


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
        if not (isinstance(result, TextResult)):
            raise AssertionError("isinstance(result, TextResult)")
        if "Alpha page one" not in result.text:
            raise AssertionError('"Alpha page one" in result.text')
        if "Beta page two" not in result.text:
            raise AssertionError('"Beta page two" in result.text')
        if result.metadata["pages"] != "1,2":
            raise AssertionError('result.metadata["pages"] == "1,2"')

    async def test_document_outline(self, doc_tools, workspace_pdf) -> None:
        result = await Call.ok(
            doc_tools["document_outline"],
            path=workspace_pdf,
            ocr_enabled=False,
            num_workers=1,
            ocr_language="rus+eng",
        )
        if not (isinstance(result, TableResult)):
            raise AssertionError("isinstance(result, TableResult)")
        pages = []
        for row in result.rows:
            pages.append(row["page"])
        if pages != [1, 2]:
            raise AssertionError("pages == [1, 2]")

    async def test_read_document_pages_subset(self, doc_tools, workspace_pdf) -> None:
        result = await Call.ok(
            doc_tools["read_document"],
            path=workspace_pdf,
            pages="2",
            ocr_enabled=False,
            num_workers=1,
            ocr_language="rus+eng",
        )
        if "Beta page two" not in result.text:
            raise AssertionError('"Beta page two" in result.text')
        if "Alpha page one" in result.text:
            raise AssertionError('"Alpha page one" not in result.text')

    async def test_search_document(self, doc_tools, workspace_pdf) -> None:
        result = await Call.ok(
            doc_tools["search_document"],
            path=workspace_pdf,
            query="Alpha",
            ocr_enabled=False,
            num_workers=1,
            ocr_language="rus+eng",
        )
        if not (isinstance(result, TableResult)):
            raise AssertionError("isinstance(result, TableResult)")
        if len(result.rows) != 2:
            raise AssertionError("len(result.rows) == 2")
        if result.rows[0]["page"] != 1:
            raise AssertionError('result.rows[0]["page"] == 1')

    async def test_missing_document_fails_loudly(self, doc_tools) -> None:
        """Нет файла — объявленный отказ парсера, а не крах процесса."""
        with pytest.raises(PayloadFailureError) as failure:
            await Call.result(
                doc_tools["read_document"],
                path="/workspace/no.pdf",
                pages="1",
                ocr_enabled=False,
                num_workers=1,
                ocr_language="rus+eng",
            )

        if failure.value.kind != "document_unreadable":
            raise AssertionError('failure.value.kind == "document_unreadable"')
        if "no.pdf" not in str(failure.value):
            raise AssertionError('"no.pdf" in str(failure.value)')


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
        if not (isinstance(result, ChartResult)):
            raise AssertionError("isinstance(result, ChartResult)")
        if result.title != "итоги":
            raise AssertionError('result.title == "итоги"')
        if result.spec["data"][0]["type"] != "bar":
            raise AssertionError('result.spec["data"][0]["type"] == "bar"')

    async def test_broken_spec_fails_loudly(self, chart_tool) -> None:
        with pytest.raises(PayloadFailureError) as caught:
            await Call.result(chart_tool, spec="не json")

        if caught.value.kind != "invalid_figure_spec":
            raise AssertionError('caught.value.kind == "invalid_figure_spec"')


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
        if result.payload["total_lines"] <= 0:
            raise AssertionError('result.payload["total_lines"] > 0')
        if result.payload["returned_lines"] > 20:
            raise AssertionError('result.payload["returned_lines"] <= 20')
        if result.payload["source_url"] != whitelisted_url:
            raise AssertionError('result.payload["source_url"] == whitelisted_url')

    async def test_grep_page(self, web_tools, whitelisted_url) -> None:
        result = await Call.ok(
            web_tools["web_grep_page"],
            url=whitelisted_url,
            pattern="Confluence",
            limit=3,
        )
        if not (isinstance(result, TableResult)):
            raise AssertionError("isinstance(result, TableResult)")
        if not (result.rows):
            raise AssertionError("result.rows")
        if "Confluence" not in result.rows[0]["content"]:
            raise AssertionError('"Confluence" in result.rows[0]["content"]')

    async def test_host_outside_whitelist(self, web_tools) -> None:
        """Отказ нового пути — исключение с kind, а не ErrorResult-успех."""
        with pytest.raises(PayloadFailureError) as caught:
            await Call.result(
                web_tools["web_fetch_page"],
                url="https://example.com/",
                as_markdown=True,
                line_offset=0,
                line_count=5,
            )

        if caught.value.kind != "unknown_host":
            raise AssertionError('caught.value.kind == "unknown_host"')
        if "whitelist" not in str(caught.value):
            raise AssertionError('"whitelist" in str(caught.value)')


class TestConfluenceTools:
    """confluence: REST-запрос и разбор ответа — целиком в песочнице."""

    async def test_spaces(self, confluence_tools) -> None:
        result = await Call.ok(confluence_tools["confluence_spaces"], limit=10)
        if not (isinstance(result, TableResult)):
            raise AssertionError("isinstance(result, TableResult)")
        if not (result.rows):
            raise AssertionError("result.rows")
        if set(result.rows[0]) < {"key", "name", "type"}:
            raise AssertionError('set(result.rows[0]) >= {"key", "name", "type"}')

    async def test_search(self, confluence_tools) -> None:
        result = await Call.ok(
            confluence_tools["confluence_search"],
            query="данные",
            limit=5,
            snippet_chars=200,
        )
        if not (result.rows):
            raise AssertionError("result.rows")
        if set(result.rows[0]) < {"page_id", "title", "space_key", "url"}:
            raise AssertionError('set(result.rows[0]) >= {"page_id", "title", "space_…')

    async def test_fetch_page(self, confluence_tools, confluence_page) -> None:
        result = await Call.ok(
            confluence_tools["confluence_fetch"],
            page_id=confluence_page["page_id"],
            as_markdown=True,
        )
        if not (isinstance(result, TextResult)):
            raise AssertionError("isinstance(result, TextResult)")
        if not (result.text.strip()):
            raise AssertionError("result.text.strip()")

    async def test_grep_page(self, confluence_tools, confluence_page) -> None:
        word = confluence_page["title"].split()[0]
        result = await Call.ok(
            confluence_tools["confluence_grep"],
            page_id=confluence_page["page_id"],
            pattern=word,
            case_insensitive=True,
            limit=3,
        )
        if not (isinstance(result, TableResult)):
            raise AssertionError("isinstance(result, TableResult)")

    async def test_unknown_page_reports_error(self, confluence_tools) -> None:
        """Несуществующая страница — объявленный отказ с kind'ом инструмента."""
        with pytest.raises(PayloadFailureError) as failure:
            await Call.result(
                confluence_tools["confluence_fetch"], page_id="0", as_markdown=True
            )

        if failure.value.kind != "confluence_request_failed":
            raise AssertionError('failure.value.kind == "confluence_request_failed"')


class TestPgTools:
    """pg: соединение, kerberos и SQL исполняются внутри песочницы."""

    async def test_list_targets(self, pg_tools) -> None:
        result = await Call.ok(pg_tools["pg_list_targets"])
        targets = []
        for row in result.rows:
            targets.append(row["connection_name"])
        if not (targets):
            raise AssertionError("targets")

    async def test_list_tables(self, pg_tools) -> None:
        result = await Call.ok(
            pg_tools["pg_list_tables"],
            connection_name="main",
            pg_schema="pg_catalog",
        )
        if not (result.rows):
            raise AssertionError("result.rows")
        if set(result.rows[0]) < {"schema", "table_name", "kind", "owner"}:
            raise AssertionError('set(result.rows[0]) >= {"schema", "table_name", "ki…')

    async def test_system_schemas_are_not_hidden(self, pg_tools) -> None:
        """Каталог не прячется: системные схемы видны наравне с остальными."""
        result = await Call.ok(pg_tools["pg_list_tables"], connection_name="main")
        schemas = set()
        for row in result.rows:
            schemas.add(row["schema"])
        if not (schemas):
            raise AssertionError("schemas")

    async def test_table_pattern_filters_by_name(self, pg_tools) -> None:
        result = await Call.ok(
            pg_tools["pg_list_tables"],
            connection_name="main",
            pg_schema="pg_catalog",
            table_pattern="pg_cl%",
        )
        if not (result.rows):
            raise AssertionError("result.rows")
        for row in result.rows:
            if not (row["table_name"].startswith("pg_cl")):
                raise AssertionError('row["table_name"].startswith("pg_cl")')

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
        if not (result.rows):
            raise AssertionError("result.rows")
        if set(result.rows[0]) < {"column_name", "type", "nullable", "primary_key"}:
            raise AssertionError('set(result.rows[0]) >= {"column_name", "type", "nul…')

    async def test_query_returns_rows(self, pg_tools) -> None:
        result = await Call.ok(
            pg_tools["pg_query"],
            connection_name="main",
            sql="select 1 as one, 'два' as two",
        )
        if not (isinstance(result, TableResult)):
            raise AssertionError("isinstance(result, TableResult)")
        if result.rows[0]["one"] != 1:
            raise AssertionError('result.rows[0]["one"] == 1')
        if result.rows[0]["two"] != "два":
            raise AssertionError('result.rows[0]["two"] == "два"')

    async def test_statement_without_rows_reports_status(self, pg_tools) -> None:
        """DDL проходит и отчитывается статусом; временная таблица живёт в сессии."""
        result = await Call.ok(
            pg_tools["pg_query"],
            connection_name="main",
            sql="create temp table integration_probe(x int)",
        )
        if not (isinstance(result, AffectedSqlResult)):
            raise AssertionError("isinstance(result, AffectedSqlResult)")
        if result.status != "CREATE TABLE":
            raise AssertionError('result.status == "CREATE TABLE"')

    async def test_copy_unloads_the_statement_as_is(self, pg_tools) -> None:
        """Формат выбирает запрос: csv-дамп доезжает как есть и помечен языком."""
        result = await Call.ok(
            pg_tools["pg_copy"],
            connection_name="main",
            sql=(
                "COPY (select 1 as one, 'два' as two) "
                "TO STDOUT WITH (FORMAT CSV, HEADER)"
            ),
        )
        if not isinstance(result, TextResult):
            raise AssertionError("isinstance(result, TextResult)")
        if result.text != "one,two\n1,два\n":
            raise AssertionError('result.text == "one,two\\n1,два\\n"')
        if result.language != "csv":
            raise AssertionError('result.language == "csv"')

    async def test_copy_decodes_in_the_connection_encoding(self, pg_tools) -> None:
        """Кириллица доезжает целой: дамп читается кодировкой подключения.

        Символ склеивается из блоков COPY, поэтому проверяется и длинная
        выгрузка — на ней граница блока рвёт многобайтовый символ.
        """
        result = await Call.ok(
            pg_tools["pg_copy"],
            connection_name="main",
            sql=("COPY (select repeat('ё', 4000) as long) TO STDOUT WITH (FORMAT CSV)"),
        )

        if "�" in result.text:
            raise AssertionError("в выгрузке остались замены битых байтов")
        if result.text.strip() != "ё" * 4000:
            raise AssertionError('result.text.strip() == "ё" * 4000')

    async def test_copy_statement_is_judged_by_postgres(self, pg_tools) -> None:
        """Стейтмент уходит как есть: приговор выносит сервер, не инструмент."""
        with pytest.raises(PayloadFailureError) as caught:
            await Call.result(
                pg_tools["pg_copy"], connection_name="main", sql="select 1"
            )

        if caught.value.kind != "sql_failed":
            raise AssertionError('caught.value.kind == "sql_failed"')

    async def test_many_statements_run_in_one_call(self, pg_tools) -> None:
        """Несколько команд через `;`: итог каждой по порядку одним набором."""
        result = await Call.ok(
            pg_tools["pg_query"],
            connection_name="main",
            sql=(
                "create temp table multi_probe(x int); "
                "insert into multi_probe values (1), (2); "
                "select count(*) as n from multi_probe;"
            ),
        )

        if not isinstance(result, MultiResult):
            raise AssertionError("isinstance(result, MultiResult)")
        kinds = [type(item).__name__ for item in result.items]
        if kinds != ["AffectedSqlResult", "AffectedSqlResult", "TableResult"]:
            raise AssertionError(f"итоги команд по порядку, получено {kinds}")

        last = result.items[-1]
        if not isinstance(last, TableResult):
            raise AssertionError("isinstance(last, TableResult)")
        if list(last.rows) != [{"n": 2}]:
            raise AssertionError('rows == [{"n": 2}]')

    async def test_failed_statement_rolls_the_set_back(self, pg_tools) -> None:
        """Набор идёт одной транзакцией: падение второй команды сносит первую."""
        with pytest.raises(PayloadFailureError):
            await Call.result(
                pg_tools["pg_query"],
                connection_name="main",
                sql=(
                    "create temp table rollback_probe(x int); "
                    "select * from no_such_table_here;"
                ),
            )

        after = await Call.result(
            pg_tools["pg_query"],
            connection_name="main",
            sql="select to_regclass('rollback_probe') is null as gone",
        )
        if list(after.rows) != [{"gone": True}]:
            raise AssertionError("таблица первой команды откачена")

    async def test_unknown_target_is_rejected(self, pg_tools) -> None:
        """Отказ нового пути — исключение с kind, а не ErrorResult-успех."""
        with pytest.raises(PayloadFailureError) as caught:
            await Call.result(
                pg_tools["pg_query"], connection_name="нет-такого", sql="select 1"
            )

        if caught.value.kind != "unknown_target":
            raise AssertionError('caught.value.kind == "unknown_target"')


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
        if stats["collection"] != kb_collection:
            raise AssertionError('stats["collection"] == kb_collection')
        if stats["indexed"] <= 0:
            raise AssertionError('stats["indexed"] > 0')
        if stats["failed"] != 0:
            raise AssertionError('stats["failed"] == 0')

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
        if stats["skipped_unchanged"] != 1:
            raise AssertionError('stats["skipped_unchanged"] == 1')
        if stats["indexed"] != 0:
            raise AssertionError('stats["indexed"] == 0')

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
        if stats["collection"] != kb_collection:
            raise AssertionError('stats["collection"] == kb_collection')
        if stats["failed"] != 0:
            raise AssertionError('stats["failed"] == 0')
        if stats["skipped_unchanged"] < 1:
            raise AssertionError('stats["skipped_unchanged"] >= 1')

    async def test_unknown_space_reports_error(self, ingest_tools) -> None:
        """Несуществующий space — объявленный отказ с kind'ом инструмента."""
        with pytest.raises(PayloadFailureError) as failure:
            await Call.result(
                ingest_tools["confluence_index_spaces"],
                space_keys=["NOSUCHSPACE"],
                prune_missing=False,
                force_update=False,
            )

        if failure.value.kind != "ingest_request_failed":
            raise AssertionError('failure.value.kind == "ingest_request_failed"')

    async def test_fetch_attachment(
        self, ingest_tools, confluence_attachment_ref
    ) -> None:
        result = await Call.ok(
            ingest_tools["confluence_attachment"],
            page_id=confluence_attachment_ref["page_id"],
            filename=confluence_attachment_ref["filename"],
        )
        if not (isinstance(result, TextResult)):
            raise AssertionError("isinstance(result, TextResult)")
        if not (result.text.strip()):
            raise AssertionError("result.text.strip()")


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
        if not (isinstance(result, TableResult)):
            raise AssertionError("isinstance(result, TableResult)")
        if not (result.rows):
            raise AssertionError("result.rows")
        found = []
        for row in result.rows:
            found.append(row["page_id"])
        if confluence_page["page_id"] not in found:
            raise AssertionError('confluence_page["page_id"] in found')

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
        if not (result.rows):
            raise AssertionError("result.rows")
        columns = set(result.rows[0])
        if not {"distance", "format_content", "page_title"} <= columns:
            raise AssertionError(f"в выдаче нет нужных колонок: {sorted(columns)}")
