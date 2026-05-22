"""Pytest fixtures для пакета boba-tool-kb (integration-mode).

ВСЕ тесты — integration: ходят в реальный postgres, реальный embeddings
endpoint, реальный Confluence. Параметры конфигурации читаются через
систему конфигурирования (BobaFlatSettings) из соответствующих секций.

Все tools self-contained: каждая фикстура `*_cfg` грузит свой
tool-конфиг из его TOML-секции (`[tool.kb.<tool_name>]` / sub-section).
Сервисы (`PostgresChunkStore`, `PostgresKnowledgeBase`, `PgFtsKnowledgeBase`,
`SqlExecutor`) строятся внутри tool-функций — фикстуры этих сервисов не
определяются на уровне conftest, потому что они зависят от конкретного
tool-конфига.

`pytest -m integration` для запуска; default-режим (`-m "not integration"`)
их исключает (см. root pyproject.toml).
"""
# pyright: reportCallIssue=false
# BobaFlatSettings()-вызовы для config-фикстур: pyright не видит, что поля
# заполняются source-loader'ом из TOML, и считает обязательные аргументы
# отсутствующими. Это runtime-фича boba.settings — статически не выразимо.

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from pydantic import Field, ValidationError

from boba.indexing import (
    PipelineContext,
    PipelineId,
    RawDocument,
)
from boba.settings import BobaFlatSettings, BobaSettingsConfigDict
from boba.tool.kb.confluence.connection import ConfluenceConnection
from boba.tool.kb.confluence.tools.cql_ingest import ConfluenceCqlIngestConfig
from boba.tool.kb.confluence.tools.cql_search import ConfluenceCqlSearchConfig
from boba.tool.kb.confluence.tools.list_spaces import ConfluenceListSpacesConfig
from boba.tool.kb.confluence.tools.page_download import (
    ConfluencePageDownloadConfig,
)
from boba.tool.kb.confluence.tools.page_ingest import ConfluencePageIngestConfig
from boba.tool.kb.confluence.tools.space_download import (
    ConfluenceSpaceDownloadConfig,
)
from boba.tool.kb.confluence.tools.space_ingest import ConfluenceSpaceIngestConfig
from boba.tool.kb.core.tools.files_ingest import FilesIngestConfig
from boba.tool.kb.core.tools.kb_search import SearchInKbConfig
from boba.tool.kb.core.tools.vector_search import VectorSearchConfig
from boba.tool.kb.fts.tools.fts_search import FtsSearchConfig
from boba.tool.kb.sql.tools.describe_table import SqlDescribeTableConfig
from boba.tool.kb.sql.tools.list_tables import SqlListTablesConfig
from boba.tool.kb.sql.tools.query import SqlQueryConfig
from boba.transport.http import HttpTransport
from boba.workspace.contract import WorkspaceId

# --------------------------------------------------------------------------- #
# KbIntegrationTestConfig — тестовая секция [test.kb]
# --------------------------------------------------------------------------- #


class KbIntegrationTestConfig(BobaFlatSettings):
    """Параметры integration-тестов KB-плагина (секция `[test.kb]`)."""

    model_config = BobaSettingsConfigDict(
        case_sensitive=False,
        extra="ignore",
        config_path="test.kb",
    )

    kb_search_query: str = Field(
        default="",
        description="Search-запрос для test_kb_search. Пусто = skip.",
    )
    kb_search_top_k: int = Field(
        default=5,
        ge=1,
        description="top_k для test_kb_search.",
    )

    confluence_page_ids: list[str] = Field(
        default_factory=list,
        description=(
            "Реальные page_id для test_confluence_page_download / page_ingest. "
            "Пусто = тесты скипаются."
        ),
    )
    confluence_space_key: str = Field(
        default="",
        description=(
            "Реальный space_key для test_confluence_space_download / "
            "space_ingest / discovery. Пусто = skip."
        ),
    )
    confluence_cql: str = Field(
        default="",
        description=(
            "Реальный CQL для test_cql_source_returns_pages_for_real_cql."
        ),
    )
    confluence_search_query: str = Field(
        default="",
        description=(
            "Plain-text запрос для test_confluence_cql_search "
            "(оборачивается в CQL внутри tool'а). Пусто = skip."
        ),
    )
    confluence_search_space: str = Field(
        default="",
        description="`space=` ограничение для test_confluence_cql_search.",
    )

    fts_query: str = Field(
        default="",
        description="Запрос для тестов FTS (`fts_search`). Пусто = skip.",
    )


# --------------------------------------------------------------------------- #
# Config-фикстуры (skip-if-not-configured) — по одной на каждый tool
# --------------------------------------------------------------------------- #


@pytest.fixture
def test_cfg() -> KbIntegrationTestConfig:
    """`KbIntegrationTestConfig` из `[test.kb]`; skip при ошибке валидации."""
    try:
        return KbIntegrationTestConfig()
    except ValidationError as e:
        pytest.skip(f"[test.kb] не сконфигурирован: {e}")


@pytest.fixture
def files_ingest_cfg() -> FilesIngestConfig:
    """`FilesIngestConfig` из `[tool.kb.files_ingest]`."""
    try:
        return FilesIngestConfig()
    except ValidationError as e:
        pytest.skip(f"[tool.kb.files_ingest] не сконфигурирован: {e}")


@pytest.fixture
def confluence_space_ingest_cfg() -> ConfluenceSpaceIngestConfig:
    try:
        return ConfluenceSpaceIngestConfig()
    except ValidationError as e:
        pytest.skip(f"[tool.kb.confluence.ingest.space] не сконфигурирован: {e}")


@pytest.fixture
def confluence_page_ingest_cfg() -> ConfluencePageIngestConfig:
    try:
        return ConfluencePageIngestConfig()
    except ValidationError as e:
        pytest.skip(f"[tool.kb.confluence.ingest.page] не сконфигурирован: {e}")


@pytest.fixture
def confluence_cql_ingest_cfg() -> ConfluenceCqlIngestConfig:
    try:
        return ConfluenceCqlIngestConfig()
    except ValidationError as e:
        pytest.skip(f"[tool.kb.confluence.ingest.cql] не сконфигурирован: {e}")


@pytest.fixture
def kb_search_cfg() -> SearchInKbConfig:
    try:
        return SearchInKbConfig()
    except ValidationError as e:
        pytest.skip(f"[tool.kb.search_in_kb] не сконфигурирован: {e}")


@pytest.fixture
def vector_search_cfg() -> VectorSearchConfig:
    try:
        return VectorSearchConfig()
    except ValidationError as e:
        pytest.skip(f"[tool.kb.search.vector] не сконфигурирован: {e}")


@pytest.fixture
def fts_search_cfg() -> FtsSearchConfig:
    try:
        return FtsSearchConfig()
    except ValidationError as e:
        pytest.skip(f"[tool.kb.search.fts] не сконфигурирован: {e}")


@pytest.fixture
def confluence_cql_search_cfg() -> ConfluenceCqlSearchConfig:
    try:
        return ConfluenceCqlSearchConfig()
    except ValidationError as e:
        pytest.skip(f"[tool.kb.confluence.search.cql] не сконфигурирован: {e}")


@pytest.fixture
def confluence_list_spaces_cfg() -> ConfluenceListSpacesConfig:
    try:
        return ConfluenceListSpacesConfig()
    except ValidationError as e:
        pytest.skip(
            f"[tool.kb.confluence.list_spaces] не сконфигурирован: {e}",
        )


@pytest.fixture
def confluence_page_download_cfg() -> ConfluencePageDownloadConfig:
    try:
        return ConfluencePageDownloadConfig()
    except ValidationError as e:
        pytest.skip(f"[tool.kb.confluence.download.page] не сконфигурирован: {e}")


@pytest.fixture
def confluence_space_download_cfg() -> ConfluenceSpaceDownloadConfig:
    try:
        return ConfluenceSpaceDownloadConfig()
    except ValidationError as e:
        pytest.skip(
            f"[tool.kb.confluence.download.space] не сконфигурирован: {e}",
        )


@pytest.fixture
def sql_query_cfg() -> SqlQueryConfig:
    try:
        return SqlQueryConfig()
    except ValidationError as e:
        pytest.skip(f"[tool.kb.sql_query] не сконфигурирован: {e}")


@pytest.fixture
def sql_list_tables_cfg() -> SqlListTablesConfig:
    try:
        return SqlListTablesConfig()
    except ValidationError as e:
        pytest.skip(f"[tool.kb.sql_list_tables] не сконфигурирован: {e}")


@pytest.fixture
def sql_describe_table_cfg() -> SqlDescribeTableConfig:
    try:
        return SqlDescribeTableConfig()
    except ValidationError as e:
        pytest.skip(f"[tool.kb.sql_describe_table] не сконфигурирован: {e}")


# --------------------------------------------------------------------------- #
# Confluence-инфраструктура для unit-тестов decoder/reader/requests
# --------------------------------------------------------------------------- #


@pytest.fixture
def confluence_connection(
    confluence_page_download_cfg: ConfluencePageDownloadConfig,
) -> ConfluenceConnection:
    """`ConfluenceConnection` — берётся из любого confluence-tool-конфига
    (выбран page_download как минимальный)."""
    return confluence_page_download_cfg.confluence


@pytest.fixture
def confluence_auth(
    confluence_connection: ConfluenceConnection,
) -> httpx.Auth | None:
    """`httpx.Auth | None` (PatAuth | BasicAuth | None для anonymous)."""
    return confluence_connection.make_auth()


@pytest.fixture
def confluence_transport(
    confluence_connection: ConfluenceConnection,
) -> HttpTransport:
    """Real HttpTransport с timeout/ssl_verify."""
    return confluence_connection.make_transport()


@pytest.fixture
def confluence_raw_doc(
    confluence_connection: ConfluenceConnection,
    confluence_auth: httpx.Auth | None,
    confluence_transport: HttpTransport,
    test_cfg: KbIntegrationTestConfig,
    pipeline_ctx: PipelineContext,
) -> RawDocument:
    """Реальный `RawDocument` с одной страницы Confluence (до декодинга)."""
    from io import BytesIO

    from boba.tool.kb.confluence.request_sources.pages import (
        ConfluencePagesRequestSource,
    )

    if not test_cfg.confluence_page_ids:
        pytest.skip("test.kb.confluence_page_ids пусто")

    page_id = test_cfg.confluence_page_ids[0]
    src = ConfluencePagesRequestSource(
        base_url=confluence_connection.base_url,
        auth=confluence_auth,
        page_ids=[page_id],
        body_format=confluence_connection.body_format,
    )
    requests = list(src.stream(pipeline_ctx))
    assert len(requests) == 1

    materialized: RawDocument | None = None
    for rd in confluence_transport.stream(pipeline_ctx, requests):
        body = rd.handle.read()
        materialized = replace(rd, handle=BytesIO(body))
    assert materialized is not None
    return materialized


@pytest.fixture
def confluence_decoded_doc(
    confluence_connection: ConfluenceConnection,
    confluence_raw_doc: RawDocument,
) -> RawDocument:
    """Реальный `RawDocument` после `ConfluenceJsonDecoder`."""
    from boba.tool.kb.confluence.decoder import ConfluenceJsonDecoder

    decoder = ConfluenceJsonDecoder(body_format=confluence_connection.body_format)
    return decoder.convert(confluence_raw_doc)


@pytest.fixture
def workspace_shell(tmp_path: Path):
    """Real `FsProjectWorkspaceShell` на pytest tmp_path."""
    from boba.agent.workspace_fs.shell import FsProjectWorkspaceShell

    return FsProjectWorkspaceShell(
        workspace_id=WorkspaceId("test"),
        root=tmp_path,
    )


# --------------------------------------------------------------------------- #
# Pipeline helpers
# --------------------------------------------------------------------------- #


@pytest.fixture
def pipeline_ctx() -> PipelineContext:
    """`PipelineContext` с тестовым `PipelineId('t')`."""
    return PipelineContext(pipeline_id=PipelineId("t"))
