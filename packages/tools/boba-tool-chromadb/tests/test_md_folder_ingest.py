"""
Интеграционные тесты `MdFolderIndexer` + dual-mode operator контрол.

Два режима:

1. CI / dev (всегда запускается):
   - `FakeDeterministicEmbedder` (детерминированный hash → вектор)
   - real ChromaDB на `tmp_path`
   - Проверяет: walk дерева, idempotency, metadata round-trip, prune,
     контракт `VectorStoreWriter`.

2. Operator-mode (включается, когда `cfg.ingest_folder` не пуст):
   - ВСЕ параметры читаются из `ChromadbPluginConfig` — единая система
     конфигурирования (`local/config.toml` + env `BOBA_TOOL__CHROMADB__*`).
     Никаких кастомных env-варсов и никакой генерации конфига в тестах:
     оператор настраивает один раз свой config.toml, дальше pytest
     использует те же значения, что и runtime-плагин.
   - real embedder через `EmbedderFactory.create` (HTTP-вызовы LiteLLM/
     OpenAI; для `embedding_model="default"` — built-in ONNX без сети).
   - Это и есть «оператор индексирует свои документы»; pytest заменяет
     CLI: дёшево писать, дёшево запускать, видны live-проблемы.

Запуск оператором:

    # либо положить в local/config.toml:
    #   [tool.chromadb]
    #   ingest_folder = "/path/to/kb"
    # либо env-override:
    BOBA_TOOL__CHROMADB__INGEST_FOLDER=/path/to/kb \\
        pytest packages/tools/boba-tool-chromadb/tests/test_md_folder_ingest.py::test_operator_real_ingest -s
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from boba.indexing.context import CollectionId
from boba.indexing.embedder import Embedder
from boba.indexing.sections import SourceId
from boba.tool.chromadb.embedder_factory import EmbedderFactory
from boba.tool.chromadb.md_chunk import MdChunkParser
from boba.tool.chromadb.md_folder_ingest import MdFolderIndexer
from boba.tool.chromadb.vector_store import ChromaVectorStore

if TYPE_CHECKING:
    # Только для type-checker'а: pytest сам резолвит фикстуру по имени;
    # импортировать `tests.conftest` напрямую в run-time нельзя — это
    # special-loaded модуль pytest'а, не пакет.
    from tests.conftest import OperatorRunSpec

COLLECTION = CollectionId("test_kb")


@pytest.fixture
def store(
    chromadb_client_persistent: Any,
    fake_embedder: Embedder[str],
) -> ChromaVectorStore:
    return ChromaVectorStore(
        client=chromadb_client_persistent,
        embedder=fake_embedder,
    )


@pytest.fixture
def indexer(store: ChromaVectorStore) -> MdFolderIndexer:
    return MdFolderIndexer(store=store)


def test_ingest_basic_fixtures_folder(
    indexer: MdFolderIndexer,
    store: ChromaVectorStore,
    fixtures_kb: Path,
) -> None:
    stats = indexer.index(
        folder=fixtures_kb,
        collection=COLLECTION,
        collection_description="Test KB",
    )
    assert stats.failed == ()
    # 6 файлов в фикстурах: 4 в корне + 1 nested + 1 no-metadata
    assert stats.indexed == _count_md_files(fixtures_kb)
    assert stats.skipped_unchanged == 0
    assert stats.pruned == 0
    info = store.collection_info(COLLECTION)
    assert info.count == stats.indexed
    assert info.description == "Test KB"


def test_ingest_is_idempotent_second_run_skips_all(
    indexer: MdFolderIndexer,
    fixtures_kb: Path,
) -> None:
    first = indexer.index(folder=fixtures_kb, collection=COLLECTION)
    second = indexer.index(folder=fixtures_kb, collection=COLLECTION)
    assert second.indexed == 0
    assert second.skipped_unchanged == first.indexed


def test_ingest_detects_content_change_and_reindexes(
    indexer: MdFolderIndexer,
    store: ChromaVectorStore,
    fixtures_kb: Path,
    tmp_path: Path,
) -> None:
    """Изменение тела файла → chunk_id тот же, content_hash другой → upsert."""
    # Копируем фикстуры в tmp, чтобы можно было модифицировать.
    work = tmp_path / "kb_work"
    shutil.copytree(fixtures_kb, work)

    indexer.index(folder=work, collection=COLLECTION)
    info_before = store.collection_info(COLLECTION)

    refund = work / "refund-policy.md"
    refund.write_text(
        refund.read_text(encoding="utf-8") + "\n\nNew content added.\n",
        encoding="utf-8",
    )
    second = indexer.index(folder=work, collection=COLLECTION)

    assert second.indexed == 1
    assert second.skipped_unchanged == _count_md_files(work) - 1
    # Размер коллекции не изменился — upsert ЗАМЕНИЛ существующий чанк.
    info_after = store.collection_info(COLLECTION)
    assert info_after.count == info_before.count


def test_ingest_prune_missing_deletes_stale_chunks(
    indexer: MdFolderIndexer,
    store: ChromaVectorStore,
    fixtures_kb: Path,
    tmp_path: Path,
) -> None:
    work = tmp_path / "kb_work"
    shutil.copytree(fixtures_kb, work)
    initial = indexer.index(folder=work, collection=COLLECTION)

    # Удаляем один файл и индексируем повторно с prune_missing.
    (work / "delivery-times.md").unlink()
    second = indexer.index(
        folder=work,
        collection=COLLECTION,
        prune_missing=True,
    )
    assert second.pruned == 1
    assert store.collection_info(COLLECTION).count == initial.indexed - 1


def test_ingest_metadata_roundtrips_via_kb_search(
    indexer: MdFolderIndexer,
    fixtures_kb: Path,
    chromadb_client_persistent: Any,
) -> None:
    """source_url + anchor доходят до kb_search через chroma metadata.

    Проверяем оба клиента: write-side (ChromaVectorStore) пишет, read-side
    (raw chromadb коллекция, имитируя kb_search) находит плоские ключи.
    """
    indexer.index(folder=fixtures_kb, collection=COLLECTION)
    coll = chromadb_client_persistent.get_collection(name=str(COLLECTION))
    # Достанем чанк refund-policy (source_id = относительный путь).
    # `source_id` пишется в Chroma metadata по wire-имени из TrackingKeys —
    # без dotted-prefix (это system-key контекста индексации).
    rel = "refund-policy.md"
    raw = coll.get(
        where={"source_id": rel},
        include=["metadatas", "documents"],
    )
    metas = raw.get("metadatas") or []
    assert metas, f"no chunk found for source_id={rel}"
    md = metas[0]
    assert md["source_url"] == "https://wiki.example.com/payments/refund"
    assert md["anchor"] == "refund-policy"
    # tags chunk хранит как `tag.<name> = True` (Chroma-specific encoding)
    assert md.get("tag.payments") is True
    assert md.get("tag.policy") is True


def test_ingest_skips_bad_file_keeps_others(
    indexer: MdFolderIndexer,
    fixtures_kb: Path,
    tmp_path: Path,
) -> None:
    """Один битый файл не должен валить индексацию всей папки."""
    work = tmp_path / "kb_work"
    shutil.copytree(fixtures_kb, work)
    (work / "broken.md").write_text(
        "No H1 here, just text.\n",
        encoding="utf-8",
    )
    stats = indexer.index(folder=work, collection=COLLECTION)
    assert len(stats.failed) == 1
    assert "broken.md" in stats.failed[0].path
    assert stats.indexed == _count_md_files(work) - 1


def test_ingest_rejects_nonexistent_folder(
    indexer: MdFolderIndexer,
    tmp_path: Path,
) -> None:
    with pytest.raises(FileNotFoundError):
        indexer.index(folder=tmp_path / "nope", collection=COLLECTION)


def test_ingest_rejects_file_path_instead_of_folder(
    indexer: MdFolderIndexer,
    fixtures_kb: Path,
) -> None:
    file_path = next(fixtures_kb.glob("*.md"))
    with pytest.raises(NotADirectoryError):
        indexer.index(folder=file_path, collection=COLLECTION)


def test_similarity_search_finds_indexed_chunk(
    indexer: MdFolderIndexer,
    store: ChromaVectorStore,
    fixtures_kb: Path,
) -> None:
    """С FakeDeterministicEmbedder семантики нет, но контракт работает:
    запрос с тем же текстом, что format_content какого-то файла, должен
    находить именно этот чанк на distance=0 (одинаковый hash → один вектор).
    """
    indexer.index(folder=fixtures_kb, collection=COLLECTION)
    parser = MdChunkParser()
    refund_text = (fixtures_kb / "refund-policy.md").read_text("utf-8")
    chunk = parser.build_chunk_from_text(
        refund_text,
        source_id=SourceId("refund-policy.md"),
    )

    hits = list(
        store.similarity_search(
            COLLECTION,
            query=chunk.format_content,
            k=3,
        ),
    )
    assert hits, "expected at least one hit"
    assert hits[0].chunk_id == chunk.chunk_id
    assert hits[0].distance == pytest.approx(0.0, abs=1e-6)


# --------------------------------------------------------------------------- #
# Operator-mode: реальное индексирование оператора пакета                     #
# --------------------------------------------------------------------------- #


def test_operator_real_ingest(operator_run: OperatorRunSpec | None) -> None:
    """Operator-mode: реальная индексация в персистентный ChromaDB.

    Включается, когда задан `BOBA_KB_INGEST_FOLDER`. Берёт persist_path и
    embedding_* параметры из настоящего `ChromadbPluginConfig` (= что
    плагин читает в run-time из `local/config.toml` + env), а не из
    специальных pytest-вырсов — это гарантирует: что проиндексировано
    оператором через тест, тем же tool'ом и ищется потом.

    Печатает stats через `print` (требует `pytest -s`), чтобы оператор
    видел результат без копания в логах.
    """
    if operator_run is None:
        pytest.skip(
            "operator-mode disabled: set [tool.chromadb] ingest_folder "
            "in config or BOBA_TOOL__CHROMADB__INGEST_FOLDER env",
        )

    import chromadb

    cfg = operator_run.cfg
    embedder = EmbedderFactory().create(
        model=cfg.embedding_model,
        base_url=cfg.embedding_base_url,
        api_key=cfg.embedding_api_key,
    )
    client = chromadb.PersistentClient(path=cfg.persist_path)
    store = ChromaVectorStore(client=client, embedder=embedder)
    indexer = MdFolderIndexer(store=store)

    stats = indexer.index(
        folder=operator_run.folder,
        collection=operator_run.collection,
        collection_description=cfg.ingest_collection_description or None,
    )

    _emit("")
    _emit(f"folder:            {stats.folder}")
    _emit(f"collection:        {stats.collection}")
    _emit(f"description:       {cfg.ingest_collection_description!r}")
    _emit(f"persist_path:      {cfg.persist_path}")
    _emit(f"embedding_model:   {cfg.embedding_model}")
    _emit(f"indexed:           {stats.indexed}")
    _emit(f"skipped unchanged: {stats.skipped_unchanged}")
    _emit(f"pruned:            {stats.pruned}")
    if stats.failed:
        _emit(f"failed:            {len(stats.failed)}")
        for f in stats.failed:
            _emit(f"  - {f.path}: {f.error}")
    assert stats.failed == (), "some files failed to index; see output above"


def _count_md_files(root: Path) -> int:
    return sum(1 for p in root.rglob("*.md") if p.is_file())


def _emit(msg: str) -> None:
    """Print stats для оператора (требует `pytest -s`).

    Обёрнутый `print` нужен, чтобы один раз отметить ruff'у `noqa: T201`
    вместо засорения вызовов в operator-mode тесте.
    """
    print(msg)  # noqa: T201
