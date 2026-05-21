"""
Интеграционный тест `kb_search`: реальный postgres + pgvector + FTS + RRF.

Гибридный retrieval: vector top-K (cosine via `<=>`) + FTS top-K
(`plainto_tsquery`+`ts_rank_cd`), склейка через RRF. Тест skip-ается,
если `PostgresPluginConfig` не сконструировался (например, не задан
обязательный `dsn`).

Параметры самого запроса не входят в plugin-конфиг (`extra="ignore"`),
поэтому передаются через отдельные env-варсы:

    BOBA_KB_SEARCH_QUERY=<строка>    # по умолчанию "test"
    BOBA_KB_SEARCH_TOP_K=<int>       # по умолчанию 5

Запуск:

    BOBA_KB_SEARCH_QUERY="как оформить возврат?" \\
        pytest packages/tools/boba-tool-postgres -m integration -s
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from boba.tool.postgres.embedder_factory import EmbedderFactory
from boba.tool.postgres.kb import PostgresKnowledgeBase
from boba.tool.postgres.kb_search import kb_search
from boba.tool.postgres.migrations import apply_bootstrap

if TYPE_CHECKING:
    # pytest резолвит conftest.py-фикстуры по имени; `tests.conftest`
    # — special-loaded модуль pytest'а, в runtime не импортируется как
    # пакет.
    from tests.conftest import KbSearchRunSpec

pytestmark = pytest.mark.integration


def test_kb_search_real(kb_search_run: KbSearchRunSpec | None) -> None:
    """Реальный kb_search на персистентном postgres оператора.

    Печатает hits через print (требует `pytest -s`) — оператор видит, что
    реально нашлось по его запросу, без копания в логах.
    """
    if kb_search_run is None:
        pytest.skip(
            "kb_search integration disabled: set [tool.postgres] dsn "
            "in config or BOBA_TOOL__POSTGRES__DSN env",
        )

    from pgvector.psycopg import register_vector
    from psycopg_pool import ConnectionPool

    cfg = kb_search_run.cfg
    embedder = EmbedderFactory().create(
        model=cfg.embedding_model,
        dim=cfg.embedding_dim,
        base_url=cfg.embedding_base_url,
        api_key=cfg.embedding_api_key,
    )
    pool: ConnectionPool[Any] = ConnectionPool(
        conninfo=cfg.dsn,
        min_size=cfg.pool_min_size,
        max_size=cfg.pool_max_size,
        configure=register_vector,
        open=True,
    )
    try:
        with pool.connection() as conn:
            apply_bootstrap(conn)

        kb = PostgresKnowledgeBase(
            pool=pool,
            embedder=embedder,
            embedding_dim=cfg.embedding_dim,
            snippet_chars=cfg.snippet_chars,
            fts_language=cfg.fts_language,
            rrf_k=cfg.rrf_k,
            rrf_pool=cfg.rrf_pool,
        )

        hits = kb_search(
            query=kb_search_run.query,
            kb=kb,
            cfg=cfg,
            top_k=kb_search_run.top_k,
        )

        _emit("")
        _emit(f"dsn:             {_mask_dsn(cfg.dsn)}")
        _emit(f"collection:      {cfg.ingest_collection}")
        _emit(f"embedding_model: {cfg.embedding_model}")
        _emit(f"embedding_dim:   {cfg.embedding_dim}")
        _emit(f"fts_language:    {cfg.fts_language}")
        _emit(f"rrf_k:           {cfg.rrf_k}")
        _emit(f"rrf_pool:        {cfg.rrf_pool}")
        _emit(f"snippet_chars:   {cfg.snippet_chars}")
        _emit(f"query:           {kb_search_run.query!r}")
        _emit(f"top_k:           {kb_search_run.top_k}")
        _emit(f"hits:            {len(hits)}")
        for i, h in enumerate(hits):
            # distance = −RRF: чем меньше, тем релевантнее.
            _emit(
                f"  [{i}] dist={h['distance']:.4f}  id={h['id']}  link={h['link']}",
            )
            _emit(f"       snippet: {h['snippet']}")

        assert isinstance(hits, list)
        for h in hits:
            assert {"id", "distance", "link", "metadata", "snippet"} <= h.keys()
            assert isinstance(h["distance"], float)
            assert isinstance(h["metadata"], dict)
            assert isinstance(h["snippet"], str)
    finally:
        pool.close()


def test_kb_search_top_k_ceiling(
    kb_search_run: KbSearchRunSpec | None,
) -> None:
    """`top_k > cfg.max_top_k` → RuntimeError. Защита от перегрузки KB."""
    if kb_search_run is None:
        pytest.skip("kb_search integration disabled")

    from pgvector.psycopg import register_vector
    from psycopg_pool import ConnectionPool

    cfg = kb_search_run.cfg
    embedder = EmbedderFactory().create(
        model=cfg.embedding_model,
        dim=cfg.embedding_dim,
        base_url=cfg.embedding_base_url,
        api_key=cfg.embedding_api_key,
    )
    pool: ConnectionPool[Any] = ConnectionPool(
        conninfo=cfg.dsn,
        min_size=1,
        max_size=1,
        configure=register_vector,
        open=True,
    )
    try:
        kb = PostgresKnowledgeBase(
            pool=pool,
            embedder=embedder,
            embedding_dim=cfg.embedding_dim,
            snippet_chars=cfg.snippet_chars,
            fts_language=cfg.fts_language,
            rrf_k=cfg.rrf_k,
            rrf_pool=cfg.rrf_pool,
        )
        with pytest.raises(RuntimeError, match="превышает max_top_k"):
            kb_search(
                query=kb_search_run.query,
                kb=kb,
                cfg=cfg,
                top_k=cfg.max_top_k + 1,
            )
    finally:
        pool.close()


def _mask_dsn(dsn: str) -> str:
    if "@" not in dsn or "://" not in dsn:
        return dsn
    scheme, rest = dsn.split("://", 1)
    creds, host = rest.split("@", 1)
    if ":" in creds:
        user, _ = creds.split(":", 1)
        return f"{scheme}://{user}:***@{host}"
    return dsn


def _emit(msg: str) -> None:
    print(msg)  # noqa: T201
