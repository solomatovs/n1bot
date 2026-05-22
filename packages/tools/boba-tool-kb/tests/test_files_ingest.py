"""Integration: `files_ingest` — FS-папка → KB-коллекция через tool-обёртку.

Вызывает сам `files_ingest(store=, dispatch_reader=, chunker=, cfg=,
prune_missing=)` — это проверяет и tool-валидаторы (`folder_not_found`,
`folder_not_a_directory`), и `store.ensure_collection`, и полный pipeline.

Общие объекты (kb_cfg, kb_store, kb_dispatch_reader, kb_chunker) приходят
из conftest-фикстур.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from boba.indexing import DispatchReader
from boba.text import StructuralChunker
from boba.tool.kb.core.config import KbConfig
from boba.tool.kb.core.tools.files_ingest import files_ingest
from boba.tool.kb.core.vector_store import PostgresVectorStore

pytestmark = pytest.mark.integration


def test_files_ingest_real(
    kb_cfg: KbConfig,
    kb_store: PostgresVectorStore,
    kb_dispatch_reader: DispatchReader[str],
    kb_chunker: StructuralChunker,
) -> None:
    """Реальный вызов `files_ingest(...)` — проверяем shape ответа и failed=0."""
    folder = Path(kb_cfg.files_folder)
    if not folder.exists() or not folder.is_dir():
        pytest.skip(
            f"files_folder {folder!s} не существует — задайте "
            "[tool.kb].files_folder в конфиге или поместите файлы в default'е "
            "./local/docs",
        )

    result = files_ingest(
        store=kb_store,
        dispatch_reader=kb_dispatch_reader,
        chunker=kb_chunker,
        cfg=kb_cfg,
        prune_missing=False,
    )

    _emit("")
    _emit(f"folder:            {result['folder']}")
    _emit(f"collection:        {result['collection']}")
    _emit(f"embedding_model:   {kb_cfg.embedding_model}")
    _emit(f"indexed:           {result['indexed']}")
    _emit(f"skipped_unchanged: {result['skipped_unchanged']}")
    _emit(f"pruned:            {result['pruned']}")
    _emit(f"failed:            {result['failed']}")
    assert {"folder", "collection", "indexed", "skipped_unchanged", "pruned",
            "failed"} <= result.keys()
    assert result["folder"] == str(folder)
    assert result["collection"] == kb_cfg.collection
    assert result["failed"] == 0, "some sources failed; see logs"


def test_files_ingest_is_idempotent_second_run_skips_all(
    kb_cfg: KbConfig,
    kb_store: PostgresVectorStore,
    kb_dispatch_reader: DispatchReader[str],
    kb_chunker: StructuralChunker,
) -> None:
    """Повторный прогон не upsert'ит ничего (content_hash совпадает)."""
    folder = Path(kb_cfg.files_folder)
    if not folder.exists() or not folder.is_dir():
        pytest.skip("files_folder отсутствует")

    _first = files_ingest(
        store=kb_store,
        dispatch_reader=kb_dispatch_reader,
        chunker=kb_chunker,
        cfg=kb_cfg,
        prune_missing=False,
    )
    second = files_ingest(
        store=kb_store,
        dispatch_reader=kb_dispatch_reader,
        chunker=kb_chunker,
        cfg=kb_cfg,
        prune_missing=False,
    )
    assert second["indexed"] == 0, (
        f"expected zero re-indexed на втором запуске, got {second['indexed']!r}"
    )


def test_files_ingest_full_cleanup_returns_pruned_count(
    kb_cfg: KbConfig,
    kb_store: PostgresVectorStore,
    kb_dispatch_reader: DispatchReader[str],
    kb_chunker: StructuralChunker,
) -> None:
    """`prune_missing=True` возвращает поле `pruned >= 0` (без падений)."""
    folder = Path(kb_cfg.files_folder)
    if not folder.exists() or not folder.is_dir():
        pytest.skip("files_folder отсутствует")

    result = files_ingest(
        store=kb_store,
        dispatch_reader=kb_dispatch_reader,
        chunker=kb_chunker,
        cfg=kb_cfg,
        prune_missing=True,
    )
    assert result["pruned"] >= 0


def test_files_ingest_missing_folder_raises(
    monkeypatch: pytest.MonkeyPatch,
    kb_cfg: KbConfig,
    kb_store: PostgresVectorStore,
    kb_dispatch_reader: DispatchReader[str],
    kb_chunker: StructuralChunker,
) -> None:
    """Tool-валидатор: несуществующий `files_folder` → `RuntimeError`."""
    monkeypatch.setattr(kb_cfg, "files_folder", "/nonexistent/path/xyz")
    with pytest.raises(RuntimeError, match="folder_not_found"):
        files_ingest(
            store=kb_store,
            dispatch_reader=kb_dispatch_reader,
            chunker=kb_chunker,
            cfg=kb_cfg,
            prune_missing=False,
        )


def _emit(msg: str) -> None:
    print(msg)  # noqa: T201
