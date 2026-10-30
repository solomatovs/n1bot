"""Каждый инструмент прогоняется по реальному конфигу (pytest -m integration).

Cgroup-лимиты сняты: pytest живёт вне делегированного cgroup (test_sandbox_cgroup).
"""

from __future__ import annotations

import base64
import json
import re
import shutil
from collections.abc import Mapping, Sequence
from importlib.resources import files
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from psycopg import sql

from boba.auth.credentials import KerberosCredentialSource, NoRefresh
from boba.config import bind
from boba.connection_broker.tickets import SectionTickets, ServiceTickets
from boba.connections.manifest import ConnectionTypes
from boba.db.pgvector.migrations import Migrations
from boba.db.postgres import AsyncPostgresPool, PostgresSchema
from boba.db.postgres.connection import PostgresConfig
from boba.stand.database import TestDatabase
from boba.stand.sandbox import plugin_launch, section_profile
from boba.stand.shell import ShellRun
from boba.stand.toolsetup import Call, ToolSetup
from boba.stand.zygote import ROOTFS_IMAGE, SandboxCgroup, ZygoteStand
from boba.tool.confluence.ingest_base import ConfluenceIngestConfig
from boba.tool.kb.search import ConfluenceCollection
from boba.tool.shell.tools import BashToolConfig
from boba.toolkit.calls import ToolCallModels
from boba.toolkit.launcher import PayloadFailureError
from boba.toolkit.result import (
    MarkdownResult,
    ShellResult,
    SqlResult,
    SqlStatement,
    TableResult,
    ToolResultBase,
    VisualResult,
)
from boba.toolrun.hosted import HostedTool, PluginSection, ToolSchema
from boba.toolrun.injected import InjectedConfig, ParamSource, StaticSection
from boba.transport.http.connection import HttpConnection

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

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


@pytest.fixture(scope="module", autouse=True)
def sandbox_ready() -> None:
    """Окружение песочницы модулю обязательно: без него тесты падают с
    причиной, а не пропускаются."""
    if shutil.which("bwrap") is None:
        raise AssertionError(
            "bwrap is not on PATH: expected runtime/third/bin of the repository "
            "in PATH (make fetch sandbox)"
        )

    if not ROOTFS_IMAGE.exists():
        raise AssertionError(
            f"sandbox image {ROOTFS_IMAGE} is missing (make fetch sandbox)"
        )

    missing = SandboxCgroup().missing()
    if missing:
        raise AssertionError(missing)


@pytest.fixture(scope="module", autouse=True)
def stop_zygotes(zygote_stand: ZygoteStand):
    """Зиготы секций гасятся после модуля, как это делает выход приложения."""
    yield
    zygote_stand.stop()


@pytest.fixture(scope="module")
def bash_tool(zygote_stand: ZygoteStand, raw_config):
    cfg = ToolSetup.config(raw_config, "tool.bash", BashToolConfig)
    launcher = ToolSetup.caller(zygote_stand, raw_config, "bash")

    return ShellRun.tool(launcher, cfg)


@pytest.fixture(scope="module")
def doc_tools(zygote_stand: ZygoteStand, raw_config):
    """doc-функции новой модели: обёртка запуска + конфиг, как в загрузчике."""
    from importlib import reload

    import boba.tool.doc.tools as doc_module

    module = reload(doc_module)

    launcher = ToolSetup.caller(zygote_stand, raw_config, "doc")
    config = InjectedConfig(StaticSection())

    return ToolSetup.launched(
        module.TOOLS, launcher, (config,), _section(raw_config, "doc")
    )


@pytest.fixture(scope="module")
def chart_tool(zygote_stand: ZygoteStand, raw_config):
    """visualize новой модели: обёртка запуска на профиле секции."""
    from importlib import reload

    import boba.tool.chart.tools as chart_module

    module = reload(chart_module)

    launcher = ToolSetup.caller(zygote_stand, raw_config, "chart")

    return ToolSetup.launched([module.visualize], launcher, ())[module.visualize.name]


@pytest.fixture(scope="module")
def web_tools(zygote_stand: ZygoteStand, raw_config):
    """web-функции новой модели: обёртка запуска + конфиг, как в загрузчике."""
    from importlib import reload

    import boba.tool.web.tools as web_module

    module = reload(web_module)

    launcher = ToolSetup.caller(zygote_stand, raw_config, "web")
    sources = (GivenConnections(), InjectedConfig(StaticSection()))

    return ToolSetup.launched(
        module.TOOLS, launcher, sources, _section(raw_config, "web")
    )


class GivenConnections(ParamSource):
    """Реализация ParamSource для тестов тел: профиль соединения тест даёт
    сам аргументом вызова, без каталога соединений и запечатывания.

    В приложении параметр-соединение на хосте — имя, а профиль подставляет
    обвязка; здесь в схему хоста ставится поле с профилем как есть, и
    обёртка запуска отдаёт его телу.
    """

    def __init__(self) -> None:
        self._schemas = ToolSchema()

    def bind_all(self, tools: Sequence[HostedTool]) -> None:
        for tool in tools:
            fields: dict[str, tuple[Any, Any]] = {}
            for param in tool.inputs.connections:
                fields[param] = (Any, None)

            tool.args_schema = self._schemas.rebuild(tool.args_schema, fields, ())


@pytest.fixture(scope="module")
def web_connection(raw_config) -> HttpConnection:
    """Соединение web-тестов: тело проверит, что URL под него попадает."""
    return ToolSetup.web_connection(raw_config)


@pytest.fixture(scope="module")
def covered_url(raw_config) -> str:
    """Адрес под хостом соединения: другие хосты тело отвергнет."""
    host = ToolSetup.web_connection(raw_config).host
    return f"https://{host}/"


@pytest.fixture(scope="module")
def confluence_tools(zygote_stand: ZygoteStand, raw_config):
    """confluence-функции новой модели: обёртка запуска + конфиг."""
    from importlib import reload

    import boba.tool.confluence.tools as confluence_module

    module = reload(confluence_module)

    launcher = ToolSetup.caller(zygote_stand, raw_config, "confluence")
    config = InjectedConfig(StaticSection())

    return ToolSetup.launched(
        module.TOOLS, launcher, (config,), _section(raw_config, "confluence")
    )


@pytest.fixture(scope="module")
def pg_connection(raw_config) -> PostgresConfig:
    """Соединение pg-тестов: то, что в бою пришло бы из таблицы."""
    return ToolSetup.pg_connection(raw_config)


@pytest.fixture(scope="module")
def pg_tools(zygote_stand: ZygoteStand, raw_config):
    """pg-функции новой модели: обёртка запуска + конфиг, как в загрузчике."""
    from importlib import reload

    import boba.tool.pg.tools as pg_module

    module = reload(pg_module)

    launcher = ToolSetup.caller(zygote_stand, raw_config, "pg")
    sources = (GivenConnections(), InjectedConfig(StaticSection()))

    return ToolSetup.launched(
        module.TOOLS, launcher, sources, _section(raw_config, "pg")
    )


@pytest.fixture(scope="module")
async def kb_store(raw_config, test_database: str) -> ConfluenceIngestConfig:
    """Конфиг ingest с хранилищем в тестовой базе набора: схема базы знаний
    готовится там боевыми миграциями, рабочая база не затрагивается."""
    cfg = bind(raw_config, path="tool.ingest", model=ConfluenceIngestConfig)
    store = cfg.model_copy(
        update={"connection": TestDatabase.config_of(cfg.connection, test_database)}
    )
    schema = sql.SQL("create schema if not exists {}").format(
        sql.Identifier(store.tables.pg_schema)
    )

    pool = AsyncPostgresPool(store.connection)
    await pool.open()
    try:
        async with pool.connection() as conn:
            await conn.execute(schema)
            schema_dir = files("boba.db.pgvector") / "schema"
            names = Migrations(store.tables).names()
            await PostgresSchema(store.tables.pg_schema).apply_files(
                conn, schema_dir, names
            )
            await Migrations(store.tables).ensure_vector_index(
                conn, store.embedding.dim
            )
    finally:
        await pool.close()

    return store


@pytest.fixture(scope="module")
async def kb_collection(kb_store: ConfluenceIngestConfig):
    """Своя коллекция на прогон в тестовой базе; после модуля убирается."""
    cfg = kb_store
    name = f"kb_it_{uuid4().hex[:8]}"
    previous = ConfluenceCollection.COLLECTION
    ConfluenceCollection.COLLECTION = name
    try:
        yield name
    finally:
        ConfluenceCollection.COLLECTION = previous
        await KbCleanup.drop(cfg, name)


class KbCleanup:
    """Уборка тестовой коллекции: чанки, реестр источников и запись коллекции."""

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
                    collection = %s
                """
            ).format(sql.Identifier(cfg.tables.pg_schema, cfg.tables.sources_table)),
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
def ingest_tools(
    zygote_stand: ZygoteStand,
    raw_config,
    kb_collection: str,
    kb_store: ConfluenceIngestConfig,
):
    """ingest-функции новой модели: обёртка запуска + конфиг прогона с
    хранилищем в тестовой базе."""
    from importlib import reload

    import boba.tool.confluence.ingest_tools as ingest_module

    module = reload(ingest_module)

    launcher = ToolSetup.caller(zygote_stand, raw_config, "ingest")
    section = _section(raw_config, "ingest", kb_collection, kb_store)

    return ToolSetup.launched(module.TOOLS, launcher, (_ticketed(),), section)


def _credentials() -> KerberosCredentialSource:
    return KerberosCredentialSource(None, NoRefresh())


def _ticketed() -> InjectedConfig:
    """Секция плагина с keytab-профилем уезжает телу с билетом вызова."""
    return InjectedConfig(
        SectionTickets(ServiceTickets(_credentials), ConnectionTypes.discover)
    )


def _section(
    raw_config: Any,
    plugin_id: str,
    collection: str = "",
    store: ConfluenceIngestConfig | None = None,
) -> PluginSection:
    """Секция плагина сырыми значениями конфига, как её читает сервис; у
    хранилища базы знаний — тестовая коллекция и база набора."""
    launch = plugin_launch(ToolSetup.sandbox_raw(raw_config), plugin_id)
    values = dict(launch.section)
    if store is not None:
        connection = dict(values["connection"])
        connection["dbname"] = store.connection.dbname
        values["connection"] = connection
        values["collection"] = collection

    return PluginSection(
        values=values, connections=launch.manifest.connections_in_config
    )


@pytest.fixture(scope="module")
def kb_tools(
    zygote_stand: ZygoteStand,
    raw_config,
    kb_collection: str,
    kb_store: ConfluenceIngestConfig,
):
    """kb-функции новой модели: обёртка запуска + конфиг, как в загрузчике;
    поиск по чанкам ходит в тестовую базу, куда пишет ingest, поиск по схеме
    ix — в индексы базы приложения."""
    from importlib import reload

    import boba.tool.kb.tools as kb_module

    module = reload(kb_module)

    launcher = ToolSetup.caller(zygote_stand, raw_config, "kb")
    section = _section(raw_config, "kb", kb_collection, kb_store)

    return ToolSetup.launched(module.TOOLS, launcher, (_ticketed(),), section)


@pytest.fixture(scope="module")
def kb_ix_tools(zygote_stand: ZygoteStand, raw_config):
    """kb-функции поиска по схеме ix: секция как в бою, индексы базы приложения."""
    import boba.tool.kb.tools as kb_module

    launcher = ToolSetup.caller(zygote_stand, raw_config, "kb")

    return ToolSetup.launched(
        kb_module.TOOLS, launcher, (_ticketed(),), _section(raw_config, "kb")
    )


@pytest.fixture(scope="module")
def workspace_image(raw_config):
    """Образ тестового пользователя: создаётся из шаблона и сносится после."""
    connection = section_profile(raw_config, "bash").render(ToolSetup.path_vars())

    workspace = connection.mounts.workspace
    if workspace is None:
        pytest.fail("у профиля bash нет workspace-образа пользователя")

    image = Path(workspace.image_of(USER_ID))
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
        command=(
            f"base64 -d > {WORKSPACE_PDF} <<'B64'\n{payload}\nB64\n"
            f"test -s {WORKSPACE_PDF}"
        ),
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
        offset=0,
    )
    for row in found.rows:
        if row["title"].count(".") == 0:
            return {
                "page_id": row["page_id"],
                "title": row["title"],
                "space_key": row["space_key"],
            }
    raise AssertionError(
        "confluence_search by 'данные' returned no page (attachments only): "
        f"{[row['title'] for row in found.rows]}"
    )


@pytest.fixture(scope="module")
async def confluence_attachment_ref(confluence_tools) -> dict[str, str]:
    """Вложение ищется по расширению; page_id страницы лежит в ссылке."""
    found = await Call.ok(
        confluence_tools["confluence_search"],
        query="docx",
        limit=20,
        snippet_chars=100,
        offset=0,
    )
    for row in found.rows:
        if not row["title"].endswith(".docx"):
            continue
        match = re.search(r"pageId=(\d+)", row["url"])
        if match is None:
            continue
        return {"page_id": match.group(1), "filename": row["title"]}
    raise AssertionError(
        "confluence_search by 'docx' returned no .docx attachment with a pageId "
        f"link: {[row['title'] for row in found.rows]}"
    )


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

        call = ToolCallModels.call_of("bash", {"command": "echo hello"})
        shown = call.markdown()

        if shown != "```bash\necho hello\n```":
            raise AssertionError('shown == "```bash\\necho hello\\n```"')
        if result.output.strip() != "hello":
            raise AssertionError(f"the step shows stdout: {result.output!r}")
        if result.exit_code != 0:
            raise AssertionError(f"exit code 0: {result.exit_code}")

    async def test_stdin_is_closed(self, bash_tool, workspace_image) -> None:
        """Команда не ждёт ввода: stdin у bash-тула — /dev/null."""
        result = await Call.ok(bash_tool, command="cat; echo done")
        if result.stdout != "done\n":
            raise AssertionError(f'result.stdout == "done", дано {result.stdout!r}')

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

        if result.output.strip() != "boom":
            raise AssertionError('result.output.strip() == "boom"')
        if result.shows_stdout:
            raise AssertionError("the step shows stderr")

    async def test_network_is_unavailable(self, bash_tool, workspace_image) -> None:
        """Профиль bash без сети: имена не резолвятся, наружу хода нет."""
        result = await Call.result(bash_tool, command="getent hosts example.com")
        if result.ok:
            raise AssertionError("not result.ok")

    async def test_reading_utilities_are_installed(
        self, bash_tool, workspace_image
    ) -> None:
        """Утилиты и python-модули чтения файлов из декларации boba-tool-shell
        есть в корне песочницы bash."""
        result = await Call.result(
            bash_tool,
            command=(
                'missing=""; for tool in pdftotext pandoc catdoc antiword unrtf '
                "xmllint sqlite3 xz bzip2 unzip 7z file curl jq convert identify "
                "ffmpeg ffprobe; do "
                'command -v "$tool" > /dev/null || missing="$missing $tool"; done; '
                'test -z "$missing" || { echo "missing:$missing"; exit 1; }; '
                'python3 -c "import pandas, openpyxl, xlrd, docx, pptx, odf, '
                'pypdf, pyarrow, lxml, charset_normalizer, yaml, PIL, pillow_heif"'
            ),
        )

        if not result.ok:
            raise AssertionError(
                "bash sandbox root: expected the reading tools and python modules "
                f"declared by boba-tool-shell, got exit {result.exit_code}: "
                f"{result.stdout!r} {result.stderr!r}"
            )

    async def test_pdf_reads_with_pdftotext(self, bash_tool, workspace_pdf) -> None:
        """PDF из образа пользователя читается poppler'ом постранично."""
        result = await Call.ok(
            bash_tool, command=f"pdftotext -f 2 -l 2 {workspace_pdf} -"
        )

        if "Beta page two" not in result.stdout:
            raise AssertionError(f"pdftotext page 2: {result.stdout!r}")
        if "Alpha page one" in result.stdout:
            raise AssertionError(f"pdftotext read page 1 too: {result.stdout!r}")

    async def test_heic_photo_converts_to_jpeg(
        self, bash_tool, workspace_image
    ) -> None:
        """Фото HEIC с iPhone открывает pillow-heif, JPEG из него читает ImageMagick."""
        result = await Call.ok(
            bash_tool,
            command=(
                "python3 - <<'PY'\n"
                "from PIL import Image\n"
                "from pillow_heif import register_heif_opener\n"
                "register_heif_opener()\n"
                "Image.new('RGB', (64, 48), (200, 30, 30)).save('photo.heic')\n"
                "with Image.open('photo.heic') as photo:\n"
                "    print(photo.format)\n"
                "    photo.convert('RGB').save('photo.jpg', 'JPEG')\n"
                "PY\n"
                "identify -format '%m %w %h\\n' photo.jpg"
            ),
        )

        if result.stdout != "HEIF\nJPEG 64 48\n":
            raise AssertionError(
                "HEIC photo in the bash sandbox: expected pillow-heif to read it as "
                f"HEIF and identify to see a 64x48 JPEG, got {result.stdout!r}"
            )

    async def test_video_frame_extracts_with_ffmpeg(
        self, bash_tool, workspace_image
    ) -> None:
        """ffmpeg пишет ролик и вынимает из него кадр, ImageMagick читает кадр."""
        result = await Call.ok(
            bash_tool,
            command=(
                "ffmpeg -v error -y -f lavfi "
                "-i testsrc=duration=1:size=160x120:rate=10 clip.mp4 && "
                "ffmpeg -v error -y -i clip.mp4 -frames:v 1 frame.jpg && "
                "ffprobe -v error -show_entries stream=codec_type -of csv=p=0 "
                "clip.mp4 && "
                "identify -format '%m %w %h\\n' frame.jpg"
            ),
        )

        if result.stdout != "video\nJPEG 160 120\n":
            raise AssertionError(
                "video in the bash sandbox: expected ffprobe to see one video stream "
                f"and a 160x120 JPEG frame, got {result.stdout!r}"
            )


class TestDocTools:
    """doc: ридеры boba-doc читают документ из образа пользователя."""

    async def test_read_document_all_pages(self, doc_tools, workspace_pdf) -> None:
        result = await Call.ok(
            doc_tools["read_document"],
            path=workspace_pdf,
            pages="1-2",
            ocr_enabled=False,
            num_workers=1,
            ocr_language="rus+eng",
        )
        if not (isinstance(result, MarkdownResult)):
            raise AssertionError("isinstance(result, MarkdownResult)")
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
            pages.append(row["number"])
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
            offset=0,
            limit=50,
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

        if failure.value.failure().error_kind != "DocumentError":
            raise AssertionError(f"failure: {failure.value.failure()!r}")
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
        if not (isinstance(result, VisualResult)):
            raise AssertionError("isinstance(result, VisualResult)")
        if result.element != VisualResult.PLOTLY:
            raise AssertionError("result.element == VisualResult.PLOTLY")
        if result.title != "итоги":
            raise AssertionError('result.title == "итоги"')
        spec = result.props[VisualResult.PLOTLY_SPEC]
        if spec["data"][0]["type"] != "bar":
            raise AssertionError('spec["data"][0]["type"] == "bar"')

    async def test_broken_spec_fails_loudly(self, chart_tool) -> None:
        with pytest.raises(PayloadFailureError) as caught:
            await Call.result(chart_tool, spec="не json")

        if caught.value.failure().error_kind != "InvalidFigureSpecError":
            raise AssertionError(f"failure: {caught.value.failure()!r}")


class TestWebTools:
    """web: HTTP-запрос и разбор HTML идут внутри песочницы."""

    async def test_fetch_page(self, web_tools, web_connection, covered_url) -> None:
        result = await Call.ok(
            web_tools["web_fetch_page"],
            url=covered_url,
            connection=web_connection,
            as_markdown=True,
            line_offset=0,
            line_count=20,
        )
        if not (isinstance(result, MarkdownResult)):
            raise AssertionError("isinstance(result, MarkdownResult)")
        if result.language != "markdown":
            raise AssertionError('result.language == "markdown"')
        if len(result.text.splitlines()) > 20:
            raise AssertionError("окно не длиннее line_count")
        if result.note is None:
            raise AssertionError("result.note is not None")
        if covered_url not in result.note:
            raise AssertionError("подпись называет источник")
        if " of " not in result.note:
            raise AssertionError("подпись называет общее число строк")

    async def test_grep_page(self, web_tools, web_connection, covered_url) -> None:
        result = await Call.ok(
            web_tools["web_grep_page"],
            url=covered_url,
            connection=web_connection,
            pattern="Confluence",
            limit=3,
        )
        if not (isinstance(result, MarkdownResult)):
            raise AssertionError("isinstance(result, MarkdownResult)")
        if "Confluence" not in result.text:
            raise AssertionError('"Confluence" in result.text')
        if ": " not in result.text:
            raise AssertionError("строки совпадений помечены ':'")
        if result.note is None:
            raise AssertionError("result.note is not None")
        if "matches:" not in result.note:
            raise AssertionError("подпись считает совпадения")

    async def test_host_outside_the_connection(self, web_tools, web_connection) -> None:
        """Проверку хоста делает тело: чужой URL — исключение с kind."""
        with pytest.raises(PayloadFailureError) as caught:
            await Call.result(
                web_tools["web_fetch_page"],
                url="https://example.com/",
                connection=web_connection,
                as_markdown=True,
                line_offset=0,
                line_count=5,
            )

        if caught.value.failure().error_kind != "UnknownHostError":
            raise AssertionError(f"failure: {caught.value.failure()!r}")
        if "outside the chosen connection" not in str(caught.value):
            msg = f"host refusal must explain the coverage: {caught.value}"
            raise AssertionError(msg)


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
            offset=0,
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
        if not (isinstance(result, MarkdownResult)):
            raise AssertionError("isinstance(result, MarkdownResult)")
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
        if not (isinstance(result, MarkdownResult)):
            raise AssertionError("isinstance(result, MarkdownResult)")
        if result.note is None:
            raise AssertionError("result.note is not None")
        if confluence_page["page_id"] not in result.note:
            raise AssertionError("подпись называет страницу")

    async def test_unknown_page_reports_error(self, confluence_tools) -> None:
        """Несуществующая страница — объявленный отказ с kind'ом инструмента."""
        with pytest.raises(PayloadFailureError) as failure:
            await Call.result(
                confluence_tools["confluence_fetch"], page_id="0", as_markdown=True
            )

        request_errors = {"HttpStatusError", "TransportError", "ConfluencePayloadError"}
        if failure.value.failure().error_kind not in request_errors:
            raise AssertionError(f"failure: {failure.value.failure()!r}")


def _rows(result: ToolResultBase) -> Sequence[Mapping[str, Any]]:
    """Строки единственной команды SQL-итога."""
    if not isinstance(result, SqlResult):
        raise AssertionError(f"SqlResult expected, got {type(result).__name__}")

    statement: SqlStatement = result.statements[0]
    if statement.rows is None:
        raise AssertionError("statement carries rows")

    return statement.rows


def _note(result: ToolResultBase) -> str:
    """Note единственной команды SQL-итога."""
    if not isinstance(result, SqlResult):
        raise AssertionError(f"SqlResult expected, got {type(result).__name__}")

    return result.statements[0].note


class TestPgTools:
    """pg: соединение, kerberos и SQL исполняются внутри песочницы."""

    async def test_list_tables(self, pg_tools, pg_connection) -> None:
        result = await Call.ok(
            pg_tools["pg_list_tables"],
            connection=pg_connection,
            pg_schema="pg_catalog",
            offset=0,
            limit=50,
        )
        if not (_rows(result)):
            raise AssertionError("_rows(result)")
        if set(_rows(result)[0]) < {"schema", "table_name", "kind", "owner"}:
            raise AssertionError(
                'set(_rows(result)[0]) >= {"schema", "table_name", "ki…'
            )

    async def test_system_schemas_are_not_hidden(self, pg_tools, pg_connection) -> None:
        """Каталог не прячется: системные схемы видны наравне с остальными."""
        result = await Call.ok(
            pg_tools["pg_list_tables"],
            connection=pg_connection,
            offset=0,
            limit=50,
        )
        schemas = set()
        for row in _rows(result):
            schemas.add(row["schema"])
        if not (schemas):
            raise AssertionError("schemas")

    async def test_table_pattern_filters_by_name(self, pg_tools, pg_connection) -> None:
        result = await Call.ok(
            pg_tools["pg_list_tables"],
            connection=pg_connection,
            pg_schema="pg_catalog",
            table_pattern="pg_cl%",
            offset=0,
            limit=50,
        )
        if not (_rows(result)):
            raise AssertionError("_rows(result)")
        for row in _rows(result):
            if not (row["table_name"].startswith("pg_cl")):
                raise AssertionError('row["table_name"].startswith("pg_cl")')

    async def test_describe_table(self, pg_tools, pg_connection) -> None:
        tables = await Call.ok(
            pg_tools["pg_list_tables"],
            connection=pg_connection,
            pg_schema="pg_catalog",
            table_pattern="pg_class",
            offset=0,
            limit=50,
        )
        first = _rows(tables)[0]
        result = await Call.ok(
            pg_tools["pg_describe_table"],
            connection=pg_connection,
            table=first["table_name"],
            pg_schema=first["schema"],
            offset=0,
            limit=50,
        )
        if not (_rows(result)):
            raise AssertionError("_rows(result)")
        if set(_rows(result)[0]) < {"column_name", "type", "nullable", "primary_key"}:
            raise AssertionError(
                'set(_rows(result)[0]) >= {"column_name", "type", "nul…'
            )

    async def test_pages_do_not_overlap(self, pg_tools, pg_connection) -> None:
        """Окно листается: вторая страница продолжает первую, а не повторяет."""
        first = await Call.ok(
            pg_tools["pg_list_tables"],
            connection=pg_connection,
            pg_schema="pg_catalog",
            offset=0,
            limit=2,
        )
        if len(_rows(first)) != 2:
            raise AssertionError(f"страница ровно по окну, дано {len(_rows(first))}")

        if "next offset=2" not in str(_note(first)):
            raise AssertionError(f"note зовёт дальше, дано {_note(first)!r}")

        second = await Call.ok(
            pg_tools["pg_list_tables"],
            connection=pg_connection,
            pg_schema="pg_catalog",
            offset=2,
            limit=2,
        )
        if "rows 3-4" not in str(_note(second)):
            raise AssertionError(f"вторая страница нумеруется, дано {_note(second)!r}")

        names = set()
        for row in _rows(first):
            names.add(row["table_name"])

        for row in _rows(second):
            if row["table_name"] in names:
                raise AssertionError(f"строка {row['table_name']!r} пришла дважды")

    async def test_query_returns_rows(self, pg_tools, pg_connection) -> None:
        result = await Call.ok(
            pg_tools["pg_query"],
            connection=pg_connection,
            sql="select 1 as one, 'два' as two",
            offset=0,
            limit=50,
        )
        if not (isinstance(result, SqlResult)):
            raise AssertionError("isinstance(result, SqlResult)")
        if _rows(result)[0]["one"] != 1:
            raise AssertionError('_rows(result)[0]["one"] == 1')
        if _rows(result)[0]["two"] != "два":
            raise AssertionError('_rows(result)[0]["two"] == "два"')

    async def test_statement_without_rows_reports_status(
        self, pg_tools, pg_connection
    ) -> None:
        """DDL проходит и отчитывается статусом; временная таблица живёт в сессии."""
        result = await Call.ok(
            pg_tools["pg_query"],
            connection=pg_connection,
            sql="create temp table integration_probe(x int)",
            offset=0,
            limit=50,
        )
        if not (isinstance(result, SqlResult)):
            raise AssertionError("isinstance(result, SqlResult)")
        if result.statements[0].status != "CREATE TABLE":
            raise AssertionError('result.statements[0].status == "CREATE TABLE"')

    async def test_many_statements_run_in_one_call(
        self, pg_tools, pg_connection
    ) -> None:
        """Несколько команд через `;`: итог каждой по порядку одним набором."""
        result = await Call.ok(
            pg_tools["pg_query"],
            connection=pg_connection,
            sql=(
                "create temp table multi_probe(x int); "
                "insert into multi_probe values (1), (2); "
                "select count(*) as n from multi_probe;"
            ),
            offset=0,
            limit=50,
        )

        if not isinstance(result, SqlResult):
            raise AssertionError("isinstance(result, SqlResult)")
        statuses = [statement.status for statement in result.statements]
        if statuses != ["CREATE TABLE", "INSERT 0 2", "SELECT 1"]:
            raise AssertionError(f"итоги команд по порядку, получено {statuses}")

        last = result.statements[-1]
        if last.rows is None:
            raise AssertionError("last.rows is not None")
        if list(last.rows) != [{"n": 2}]:
            raise AssertionError('rows == [{"n": 2}]')

    async def test_failed_statement_rolls_the_set_back(
        self, pg_tools, pg_connection
    ) -> None:
        """Набор идёт одной транзакцией: падение второй команды сносит первую."""
        with pytest.raises(PayloadFailureError):
            await Call.result(
                pg_tools["pg_query"],
                connection=pg_connection,
                sql=(
                    "create temp table rollback_probe(x int); "
                    "select * from no_such_table_here;"
                ),
                offset=0,
                limit=50,
            )

        after = await Call.result(
            pg_tools["pg_query"],
            connection=pg_connection,
            sql="select to_regclass('rollback_probe') is null as gone",
            offset=0,
            limit=50,
        )
        if list(_rows(after)) != [{"gone": True}]:
            raise AssertionError("таблица первой команды откачена")


def _ingest_lines(result) -> tuple[dict[str, Any], dict[str, Any]]:
    """Отчёт ingest: строка страниц и строка вложений по имени вида."""
    by_kind: dict[str, Any] = {}
    for row in result.rows:
        by_kind[row["kind"]] = row

    return by_kind["pages"], by_kind["attachments"]


class TestIngestTools:
    """ingest: обход Confluence, чтение вложений и запись в KB — в песочнице."""

    async def test_index_pages(
        self, ingest_tools, confluence_page, kb_collection
    ) -> None:
        result = await Call.ok(
            ingest_tools["confluence_index_page"],
            page_id=confluence_page["page_id"],
            attachments=True,
        )
        pages, attachments = _ingest_lines(result)
        if kb_collection not in (result.note or ""):
            raise AssertionError(f"collection in the note: {result.note}")
        if pages["found"] != 1:
            raise AssertionError(f"one page found: {pages}")
        if pages["chunks"] <= 0:
            raise AssertionError(f"page chunks written: {pages}")
        if pages["failed"] != 0 or attachments["failed"] != 0:
            raise AssertionError(f"nothing failed: {pages}, {attachments}")

    async def test_index_spaces(
        self, ingest_tools, confluence_page, kb_collection
    ) -> None:
        """Обход целого space'а: страницы уже в коллекции — переиндексации нет."""
        result = await Call.ok(
            ingest_tools["confluence_index_space"],
            space_key=confluence_page["space_key"],
        )
        pages, attachments = _ingest_lines(result)
        if kb_collection not in (result.note or ""):
            raise AssertionError(f"collection in the note: {result.note}")
        if pages["failed"] != 0 or attachments["failed"] != 0:
            raise AssertionError(f"nothing failed: {pages}, {attachments}")
        if pages["unchanged"] < 1:
            raise AssertionError(f"pages already in the index: {pages}")

    async def test_unknown_space_reports_error(self, ingest_tools) -> None:
        """Несуществующий space — объявленный отказ с kind'ом инструмента."""
        with pytest.raises(PayloadFailureError) as failure:
            await Call.result(
                ingest_tools["confluence_index_space"],
                space_key="NOSUCHSPACE",
            )

        request_errors = {"HttpStatusError", "TransportError", "ConfluencePayloadError"}
        if failure.value.failure().error_kind not in request_errors:
            raise AssertionError(f"failure: {failure.value.failure()!r}")

    async def test_fetch_attachment(
        self, ingest_tools, confluence_attachment_ref
    ) -> None:
        result = await Call.ok(
            ingest_tools["confluence_attachment"],
            page_id=confluence_attachment_ref["page_id"],
            filename=confluence_attachment_ref["filename"],
        )
        if not (isinstance(result, MarkdownResult)):
            raise AssertionError("isinstance(result, MarkdownResult)")
        if not (result.text.strip()):
            raise AssertionError("result.text.strip()")


class TestKbTools:
    """kb: эмбеддинг и SQL идут в песочнице, ищут по свежему индексу."""

    async def test_fts_search_finds_indexed_page(
        self, kb_tools, ingest_tools, confluence_page
    ) -> None:
        await Call.ok(
            ingest_tools["confluence_index_page"],
            page_id=confluence_page["page_id"],
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
            ingest_tools["confluence_index_page"],
            page_id=confluence_page["page_id"],
        )
        result = await Call.ok(
            kb_tools["kb_vector_search"], query=confluence_page["title"], top_k=5
        )
        if not (result.rows):
            raise AssertionError("result.rows")
        columns = set(result.rows[0])
        if not {"distance", "format_content", "page_title"} <= columns:
            raise AssertionError(f"в выдаче нет нужных колонок: {sorted(columns)}")


class TestKbIxTools:
    """kb по схеме ix: эмбеддинг и SQL идут в песочнице, ищут по индексам dev-стенда."""

    async def test_catalog_lists_surfaces(self, kb_ix_tools) -> None:
        result = await Call.ok(kb_ix_tools["kb_catalog2"])
        if not (isinstance(result, TableResult)):
            raise AssertionError("isinstance(result, TableResult)")
        surfaces = []
        for row in result.rows:
            surfaces.append(row["surface"])
        if "cfl_page" not in surfaces:
            raise AssertionError(f"cfl_page in {surfaces}")

    async def test_fts_search_then_node_reads_the_page(self, kb_ix_tools) -> None:
        found = await Call.ok(
            kb_ix_tools["kb_fts_search2"],
            query="page",
            surfaces=["cfl_page"],
            aspects=["title", "body"],
            offset=0,
            limit=3,
        )
        if not (found.rows):
            raise AssertionError("found.rows")
        columns = set(found.rows[0])
        if not {"node_id", "surface", "url", "score", "aspect", "snippet"} <= columns:
            raise AssertionError(f"в выдаче нет нужных колонок: {sorted(columns)}")

        node_id = int(found.rows[0]["node_id"])
        card = await Call.ok(
            kb_ix_tools["kb_node2"], node_id=node_id, aspects=["title"]
        )
        if f"node {node_id}" not in card.text:
            raise AssertionError(f"node heading in {card.text[:200]!r}")

    async def test_vector_search_returns_hits(self, kb_ix_tools) -> None:
        result = await Call.ok(
            kb_ix_tools["kb_vector_search2"],
            query="how to configure",
            offset=0,
            limit=3,
        )
        if not (result.rows):
            raise AssertionError("result.rows")
