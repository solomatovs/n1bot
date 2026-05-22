"""Integration: `files_ingest` — FS-папка → KB-коллекция.

Self-contained tool: получает `cfg: FilesIngestConfig` + `dispatch_reader`
(stateless reader из providers). Сервисы (chunk_store / collections_store /
embedder / chunker) строятся внутри tool-функции из cfg.
"""
# pyright: reportCallIssue=false

from __future__ import annotations

from pathlib import Path

import pytest

from boba.html import HtmlReader
from boba.indexing import DispatchReader, ReaderId
from boba.kbdoc import KbDocReader
from boba.tool.kb.core.tools.files_ingest import FilesIngestConfig, files_ingest
from boba.transport.fs import FsKeys

pytestmark = pytest.mark.integration


@pytest.fixture
def kb_dispatch_reader() -> DispatchReader[str]:
    """DispatchReader для files_ingest: md → KbDocReader, html/htm → HtmlReader."""
    return DispatchReader(
        by=FsKeys.SUFFIX,
        routes={
            "md": KbDocReader(),
            "html": HtmlReader(),
            "htm": HtmlReader(),
        },
        reader_id=ReaderId("kb-dispatch"),
    )


def test_files_ingest_real(
    files_ingest_cfg: FilesIngestConfig,
    kb_dispatch_reader: DispatchReader[str],
) -> None:
    """Реальный вызов `files_ingest(...)` — проверяем shape ответа и failed=0."""
    folder = Path(files_ingest_cfg.folder)
    if not folder.exists() or not folder.is_dir():
        pytest.skip(
            f"folder {folder!s} не существует — задайте "
            "[tool.kb.files_ingest].folder в конфиге",
        )

    result = files_ingest(cfg=files_ingest_cfg, dispatch_reader=kb_dispatch_reader)

    _emit("")
    _emit(f"folder:            {result['folder']}")
    _emit(f"collection:        {result['collection']}")
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
    assert result["collection"] == files_ingest_cfg.collection
    assert result["failed"] == 0, "some sources failed; see logs"


def test_files_ingest_is_idempotent_second_run_skips_all(
    files_ingest_cfg: FilesIngestConfig,
    kb_dispatch_reader: DispatchReader[str],
) -> None:
    """Повторный прогон не upsert'ит ничего (content_hash совпадает)."""
    folder = Path(files_ingest_cfg.folder)
    if not folder.exists() or not folder.is_dir():
        pytest.skip("[tool.kb.files_ingest].folder отсутствует")

    _first = files_ingest(cfg=files_ingest_cfg, dispatch_reader=kb_dispatch_reader)
    second = files_ingest(cfg=files_ingest_cfg, dispatch_reader=kb_dispatch_reader)
    assert second["indexed"] == 0, (
        f"expected zero re-indexed на втором запуске, got {second['indexed']!r}"
    )


def test_files_ingest_full_cleanup_returns_pruned_count(
    files_ingest_cfg: FilesIngestConfig,
    kb_dispatch_reader: DispatchReader[str],
) -> None:
    """`prune=True` в конфиге возвращает поле `pruned >= 0` (без падений)."""
    folder = Path(files_ingest_cfg.folder)
    if not folder.exists() or not folder.is_dir():
        pytest.skip("[tool.kb.files_ingest].folder отсутствует")

    files_ingest_cfg.prune = True
    result = files_ingest(cfg=files_ingest_cfg, dispatch_reader=kb_dispatch_reader)
    assert result["pruned"] >= 0


def test_files_ingest_missing_folder_raises(
    monkeypatch: pytest.MonkeyPatch,
    files_ingest_cfg: FilesIngestConfig,
    kb_dispatch_reader: DispatchReader[str],
) -> None:
    """Tool-валидатор: несуществующий `folder` → `RuntimeError`."""
    monkeypatch.setattr(files_ingest_cfg, "folder", "/nonexistent/path/xyz")
    with pytest.raises(RuntimeError, match="folder_not_found"):
        files_ingest(cfg=files_ingest_cfg, dispatch_reader=kb_dispatch_reader)


def _emit(msg: str) -> None:
    print(msg)  # noqa: T201
