"""Integration: `files_ingest` — FS-папка → KB-коллекция через tool-обёртку.

Вызывает сам `ingest_files(chunk_store=, collections_store=, dispatch_reader=,
chunker=, cfg=)` — это проверяет и tool-валидаторы (`folder_not_found`,
`folder_not_a_directory`), и `collections_store.ensure_collection`, и
полный pipeline. `prune_missing` теперь не параметр tool'а — оператор
включает его через `[tool.kb.files_ingest].prune=true`.

Общие объекты (files_cfg, kb_store, kb_collections_store, kb_dispatch_reader,
kb_chunker) приходят из conftest-фикстур.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from boba.indexing import DispatchReader
from boba.text import StructuralChunker
from boba.tool.kb.core.files_ingest_config import IngestFilesConfig
from boba.tool.kb.core.postgres_store import (
    PostgresChunkStore,
    PostgresCollectionsStore,
)
from boba.tool.kb.core.tools.ingest_files import ingest_files

if TYPE_CHECKING:
    from boba.provider.openai import OpenAICompatEmbedder
    from boba.tool.kb.core.embedding_config import EmbeddingConfig

pytestmark = pytest.mark.integration


def test_files_ingest_real(  # noqa: PLR0913 — integration test
    files_cfg: IngestFilesConfig,
    embedding_cfg: EmbeddingConfig,
    kb_store: PostgresChunkStore,
    kb_collections_store: PostgresCollectionsStore,
    kb_embedder: OpenAICompatEmbedder,
    kb_dispatch_reader: DispatchReader[str],
    kb_chunker: StructuralChunker,
) -> None:
    """Реальный вызов `files_ingest(...)` — проверяем shape ответа и failed=0."""
    folder = Path(files_cfg.folder)
    if not folder.exists() or not folder.is_dir():
        pytest.skip(
            f"folder {folder!s} не существует — задайте "
            "[tool.kb.files].folder в конфиге или поместите файлы в default'е "
            "./local/docs",
        )

    result = ingest_files(
        chunk_store=kb_store,
        collections_store=kb_collections_store,
        embedder=kb_embedder,
        dispatch_reader=kb_dispatch_reader,
        chunker=kb_chunker,
        cfg=files_cfg,
    )

    _emit("")
    _emit(f"folder:            {result['folder']}")
    _emit(f"collection:        {result['collection']}")
    _emit(f"embedding_model:   {embedding_cfg.model}")
    _emit(f"indexed:           {result['indexed']}")
    _emit(f"skipped_unchanged: {result['skipped_unchanged']}")
    _emit(f"pruned:            {result['pruned']}")
    _emit(f"failed:            {result['failed']}")
    assert {
        "folder",
        "collection",
        "indexed",
        "skipped_unchanged",
        "pruned",
        "failed",
    } <= result.keys()
    assert result["folder"] == str(folder)
    assert result["collection"] == files_cfg.collection
    assert result["failed"] == 0, "some sources failed; see logs"


def test_files_ingest_is_idempotent_second_run_skips_all(  # noqa: PLR0913
    files_cfg: IngestFilesConfig,
    kb_store: PostgresChunkStore,
    kb_collections_store: PostgresCollectionsStore,
    kb_embedder: OpenAICompatEmbedder,
    kb_dispatch_reader: DispatchReader[str],
    kb_chunker: StructuralChunker,
) -> None:
    """Повторный прогон не upsert'ит ничего (content_hash совпадает)."""
    folder = Path(files_cfg.folder)
    if not folder.exists() or not folder.is_dir():
        pytest.skip("[tool.kb.files].folder отсутствует")

    _first = ingest_files(
        chunk_store=kb_store,
        collections_store=kb_collections_store,
        embedder=kb_embedder,
        dispatch_reader=kb_dispatch_reader,
        chunker=kb_chunker,
        cfg=files_cfg,
    )
    second = ingest_files(
        chunk_store=kb_store,
        collections_store=kb_collections_store,
        embedder=kb_embedder,
        dispatch_reader=kb_dispatch_reader,
        chunker=kb_chunker,
        cfg=files_cfg,
    )
    assert second["indexed"] == 0, (
        f"expected zero re-indexed на втором запуске, got {second['indexed']!r}"
    )


def test_files_ingest_full_cleanup_returns_pruned_count(  # noqa: PLR0913
    files_cfg: IngestFilesConfig,
    kb_store: PostgresChunkStore,
    kb_collections_store: PostgresCollectionsStore,
    kb_embedder: OpenAICompatEmbedder,
    kb_dispatch_reader: DispatchReader[str],
    kb_chunker: StructuralChunker,
) -> None:
    """`prune=True` в конфиге возвращает поле `pruned >= 0` (без падений)."""
    folder = Path(files_cfg.folder)
    if not folder.exists() or not folder.is_dir():
        pytest.skip("[tool.kb.files_ingest].folder отсутствует")

    files_cfg.prune = True
    result = ingest_files(
        chunk_store=kb_store,
        collections_store=kb_collections_store,
        embedder=kb_embedder,
        dispatch_reader=kb_dispatch_reader,
        chunker=kb_chunker,
        cfg=files_cfg,
    )
    assert result["pruned"] >= 0


def test_files_ingest_missing_folder_raises(  # noqa: PLR0913 — integration test
    monkeypatch: pytest.MonkeyPatch,
    files_cfg: IngestFilesConfig,
    kb_store: PostgresChunkStore,
    kb_collections_store: PostgresCollectionsStore,
    kb_embedder: OpenAICompatEmbedder,
    kb_dispatch_reader: DispatchReader[str],
    kb_chunker: StructuralChunker,
) -> None:
    """Tool-валидатор: несуществующий `folder` → `RuntimeError`."""
    monkeypatch.setattr(files_cfg, "folder", "/nonexistent/path/xyz")
    with pytest.raises(RuntimeError, match="folder_not_found"):
        ingest_files(
            chunk_store=kb_store,
            collections_store=kb_collections_store,
            embedder=kb_embedder,
            dispatch_reader=kb_dispatch_reader,
            chunker=kb_chunker,
            cfg=files_cfg,
        )


def _emit(msg: str) -> None:
    print(msg)  # noqa: T201
