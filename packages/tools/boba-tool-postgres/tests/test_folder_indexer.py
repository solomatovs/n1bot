"""
Интеграционный тест operator-mode индексации в postgres.

Включается, когда `cfg.ingest_folder` задан в `[tool.postgres]`. Берёт
dsn и embedding_* из настоящего `PostgresPluginConfig`. Использует тот
же граф, что и provider'ы в run-time (PostgresVectorStore поверх
ConnectionPool + OpenAIEmbedder), поэтому что проиндексировано тестом —
то же будет видно через kb_search.

Запуск оператором:

    # либо положить в local/config.toml:
    #   [tool.postgres]
    #   dsn = "postgres://postgres:***@127.0.0.1:5432/n1bot"
    #   embedding_model = "..."
    #   embedding_dim = 1024
    #   embedding_base_url = "..."
    #   ingest_folder = "/path/to/kb"
    #   ingest_collection = "knowledge_base"
    # либо env-override:
    BOBA_TOOL__POSTGRES__INGEST_FOLDER=/path/to/kb \\
        pytest packages/tools/boba-tool-postgres -m integration -s
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from boba.tool.postgres.embedder_factory import EmbedderFactory
from boba.tool.postgres.folder_indexer import FolderIndexer
from boba.tool.postgres.html_chunk import HtmlChunkParser
from boba.tool.postgres.md_chunk import MdChunkParser
from boba.tool.postgres.migrations import apply_bootstrap
from boba.tool.postgres.vector_store import PostgresVectorStore

if TYPE_CHECKING:
    # pytest резолвит conftest.py-фикстуры по имени; `tests.conftest`
    # — special-loaded модуль pytest'а, в runtime не импортируется как
    # пакет.
    from tests.conftest import OperatorRunSpec

pytestmark = pytest.mark.integration


def test_operator_real_ingest(operator_run: OperatorRunSpec | None) -> None:
    """Operator-mode: реальная индексация в postgres + pgvector.

    Печатает stats через `print` (требует `pytest -s`), чтобы оператор
    видел результат без копания в логах.
    """
    if operator_run is None:
        pytest.skip(
            "operator-mode disabled: set [tool.postgres] ingest_folder + dsn "
            "в config или BOBA_TOOL__POSTGRES__* env",
        )

    from pgvector.psycopg import register_vector
    from psycopg_pool import ConnectionPool

    cfg = operator_run.cfg
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

        store = PostgresVectorStore(
            pool=pool,
            embedder=embedder,
            embedding_dim=cfg.embedding_dim,
        )
        indexer = FolderIndexer(
            store=store,
            parsers=[MdChunkParser(), HtmlChunkParser()],
        )

        stats = indexer.index(
            folder=operator_run.folder,
            collection=operator_run.collection,
            collection_description=cfg.ingest_collection_description or None,
        )

        _emit("")
        _emit(f"folder:            {stats.folder}")
        _emit(f"collection:        {stats.collection}")
        _emit(f"description:       {cfg.ingest_collection_description!r}")
        _emit(f"dsn:               {_mask_dsn(cfg.dsn)}")
        _emit(f"embedding_model:   {cfg.embedding_model}")
        _emit(f"embedding_dim:     {cfg.embedding_dim}")
        _emit(f"indexed:           {stats.indexed}")
        _emit(f"skipped unchanged: {stats.skipped_unchanged}")
        _emit(f"pruned:            {stats.pruned}")
        if stats.failed:
            _emit(f"failed:            {len(stats.failed)}")
            for f in stats.failed:
                _emit(f"  - {f.path}: {f.error}")
        assert stats.failed == (), "some files failed to index; see output above"
    finally:
        pool.close()


def test_ingest_is_idempotent_second_run_skips_all(
    operator_run: OperatorRunSpec | None,
) -> None:
    """Повторный прогон ingest'а не индексирует ничего (content_hash совпадает).

    Контракт MdFolderIndexer: при неизменном file body chunk_id +
    content_hash детерминированны, partition_for_upsert пропускает чанк.
    """
    if operator_run is None:
        pytest.skip("operator-mode disabled (см. test_operator_real_ingest)")

    from pgvector.psycopg import register_vector
    from psycopg_pool import ConnectionPool

    cfg = operator_run.cfg
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
        store = PostgresVectorStore(
            pool=pool,
            embedder=embedder,
            embedding_dim=cfg.embedding_dim,
        )
        indexer = FolderIndexer(
            store=store,
            parsers=[MdChunkParser(), HtmlChunkParser()],
        )

        first = indexer.index(
            folder=operator_run.folder,
            collection=operator_run.collection,
        )
        second = indexer.index(
            folder=operator_run.folder,
            collection=operator_run.collection,
        )
        assert second.indexed == 0, (
            f"expected zero re-indexed, got {second.indexed} "
            f"(first indexed={first.indexed})"
        )
        assert second.skipped_unchanged == first.indexed + first.skipped_unchanged
    finally:
        pool.close()


def _mask_dsn(dsn: str) -> str:
    """Скрыть пароль в DSN для печати."""
    if "@" not in dsn or "://" not in dsn:
        return dsn
    scheme, rest = dsn.split("://", 1)
    creds, host = rest.split("@", 1)
    if ":" in creds:
        user, _ = creds.split(":", 1)
        return f"{scheme}://{user}:***@{host}"
    return dsn


def _emit(msg: str) -> None:
    """Print stats для оператора (требует `pytest -s`)."""
    print(msg)  # noqa: T201
