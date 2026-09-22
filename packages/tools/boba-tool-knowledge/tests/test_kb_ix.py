"""Инструменты kb на живой схеме ix dev-стенда (pytest -m integration).

Тела зовутся напрямую с конфигом [tool.kb] приложения: подключение, схема и
эмбеддер те же, что в чате. Заголовок для запросов берётся из самого индекса,
чтобы тест не зависел от конкретного наполнения базы.

Ошибки: своих не выпускает; расхождение с ожиданием — падение теста.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from omegaconf import OmegaConf
from psycopg import sql

from boba.config import bind
from boba.db.postgres import PayloadPostgres
from boba.ix_core.indexes import IndexKind
from boba.ix_core.registry import IxRegistry
from boba.ix_core.search import IxSearchError
from boba.tool.kb.kb import KbToolConfig
from boba.tool.kb.tools import (
    kb_catalog2,
    kb_fts_search2,
    kb_node2,
    kb_trgm_search2,
    kb_vector_search2,
)
from boba.toolkit.entry import ToolMain
from boba.toolkit.result import MarkdownResult, TableResult

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

PAGE = "cfl_page"
TITLE = "title"
BODY = "body"


class Body:
    """Тело инструмента по объявлению: корутина без обёртки запуска."""

    @staticmethod
    def of(declared: Any) -> Callable[..., Awaitable[Any]]:
        body = ToolMain.toolset(declared)[0].coroutine
        if body is None:
            raise AssertionError("body is not None")

        return body


@pytest.fixture(scope="module")
def kb_cfg(raw_config) -> KbToolConfig:
    """Конфиг с процессным запуском: кэш моделей эмбеддинга берётся с хоста."""
    copied = raw_config.copy()
    OmegaConf.update(copied, "env.tool_launcher", "process")

    return bind(copied, path="tool.kb", model=KbToolConfig)


@pytest.fixture(scope="module")
async def page_title(kb_cfg: KbToolConfig) -> str:
    """Заголовок любой проиндексированной страницы из полнотекстовой таблицы."""
    conn = await PayloadPostgres.connect_config(kb_cfg.connection)
    async with conn:
        registry = await IxRegistry(kb_cfg.db_schema).read(conn)
        tables = registry.tables_of(IndexKind.FTS)
        if not tables:
            pytest.skip("no fts table in the registry")

        query = sql.SQL(
            "select f.content from {}.{} f "
            "where f.surface = %(surface)s and f.aspect = %(aspect)s "
            "order by f.node_id limit 1"
        ).format(sql.Identifier(kb_cfg.db_schema), tables[0].ident())
        cur = await conn.execute(query, {"surface": PAGE, "aspect": TITLE})
        row = await cur.fetchone()

    if row is None:
        pytest.skip("no indexed cfl_page title on the stand")

    return str(row[0])


class TestCatalog:
    async def test_lists_indexed_surfaces_with_aspects(
        self, kb_cfg: KbToolConfig
    ) -> None:
        result = await Body.of(kb_catalog2)(cfg=kb_cfg)
        if not isinstance(result, TableResult):
            raise AssertionError("isinstance(result, TableResult)")

        by_name = {row["surface"]: row for row in result.rows}
        if PAGE not in by_name:
            raise AssertionError(f"{PAGE} in {sorted(by_name)}")

        aspects = str(by_name[PAGE]["aspects"])
        if f"{TITLE} (ident:" not in aspects:
            raise AssertionError(f"title aspect with its class in {aspects!r}")

        if "fts" not in aspects:
            raise AssertionError(f"fts coverage in {aspects!r}")

        if result.note is None:
            raise AssertionError("note with the aspect dictionary")

        if f"- {BODY} [description]" not in result.note:
            raise AssertionError(f"body aspect in note {result.note!r}")


class TestSearch:
    async def test_fts_finds_page_by_title_within_surface(
        self, kb_cfg: KbToolConfig, page_title: str
    ) -> None:
        result = await Body.of(kb_fts_search2)(
            query=f'"{page_title}"',
            surfaces=[PAGE],
            aspects=[TITLE],
            top_k=5,
            cfg=kb_cfg,
        )
        if not result.rows:
            raise AssertionError(f"rows for title {page_title!r}")

        for row in result.rows:
            if row["surface"] != PAGE:
                raise AssertionError(f"surface {row['surface']} leaked past filter")

            if row["aspect"] != TITLE:
                raise AssertionError(f"aspect {row['aspect']} leaked past filter")

        if not isinstance(result.rows[0]["node_id"], int):
            raise AssertionError("node_id is an int")

    async def test_trgm_finds_page_by_title_word(
        self, kb_cfg: KbToolConfig, page_title: str
    ) -> None:
        word = max(page_title.split(), key=len)
        result = await Body.of(kb_trgm_search2)(
            query=word, surfaces=[PAGE], aspects=[], top_k=5, cfg=kb_cfg
        )
        if not result.rows:
            raise AssertionError(f"rows for word {word!r}")

    async def test_vector_returns_hits_with_distance(
        self, kb_cfg: KbToolConfig, page_title: str
    ) -> None:
        result = await Body.of(kb_vector_search2)(
            query=page_title, surfaces=[], aspects=[], top_k=3, cfg=kb_cfg
        )
        if not result.rows:
            raise AssertionError("rows")

        scores = [row["score"] for row in result.rows]
        if scores != sorted(scores):
            raise AssertionError(f"vector hits ascend by distance: {scores}")

    async def test_unknown_surface_is_rejected(self, kb_cfg: KbToolConfig) -> None:
        with pytest.raises(IxSearchError, match="known are"):
            await Body.of(kb_fts_search2)(
                query="anything", surfaces=["no_such"], aspects=[], top_k=1, cfg=kb_cfg
            )

    async def test_empty_result_carries_a_hint(self, kb_cfg: KbToolConfig) -> None:
        result = await Body.of(kb_fts_search2)(
            query="qzxvbnmqwertyuiopz", surfaces=[], aspects=[], top_k=1, cfg=kb_cfg
        )
        if result.rows:
            raise AssertionError("no rows for gibberish")

        if result.note is None:
            raise AssertionError("note hints at kb_catalog2")


class TestNode:
    async def test_reads_texts_and_path_of_found_page(
        self, kb_cfg: KbToolConfig, page_title: str
    ) -> None:
        found = await Body.of(kb_fts_search2)(
            query=f'"{page_title}"',
            surfaces=[PAGE],
            aspects=[TITLE],
            top_k=1,
            cfg=kb_cfg,
        )
        node_id = int(found.rows[0]["node_id"])

        result = await Body.of(kb_node2)(node_id=node_id, aspects=[], cfg=kb_cfg)
        if not isinstance(result, MarkdownResult):
            raise AssertionError("isinstance(result, MarkdownResult)")

        if f"# {PAGE} node {node_id}" not in result.text:
            raise AssertionError(f"heading in {result.text[:200]!r}")

        if f"## {TITLE} (ident)" not in result.text:
            raise AssertionError("title section")

        if "path: " not in result.text:
            raise AssertionError("path through the space")

        only = await Body.of(kb_node2)(node_id=node_id, aspects=[TITLE], cfg=kb_cfg)
        if f"## {BODY} " in only.text:
            raise AssertionError("body excluded by the aspects filter")

    async def test_truncates_to_the_configured_limit(
        self, kb_cfg: KbToolConfig, page_title: str
    ) -> None:
        found = await Body.of(kb_fts_search2)(
            query=f'"{page_title}"',
            surfaces=[PAGE],
            aspects=[TITLE],
            top_k=1,
            cfg=kb_cfg,
        )
        node_id = int(found.rows[0]["node_id"])
        small = kb_cfg.model_copy(update={"max_result_chars": 300})

        result = await Body.of(kb_node2)(node_id=node_id, aspects=[], cfg=small)
        if len(result.text) > 300:
            raise AssertionError(f"{len(result.text)} chars over the limit")

        if result.note is None:
            raise AssertionError("truncation note")
