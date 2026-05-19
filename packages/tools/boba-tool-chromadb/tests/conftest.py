"""Pytest fixtures для пакета boba-tool-chromadb."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from boba.indexing.context import CollectionId
from boba.indexing.embedder import Embedder
from boba.tool.chromadb.config import ChromadbPluginConfig


@pytest.fixture
def fixtures_kb() -> Path:
    """Папка с подготовленными .md фикстурами (мульти-язычная)."""
    return Path(__file__).parent / "fixtures" / "kb"


class _FakeDeterministicEmbedder(Embedder[str]):
    """Простой embedder для контракт-тестов: hash → 64-dim вектор.

    Не проверяет реальную семантику (одинаковый запрос находит одинаковый
    документ, но 'возврат денег' != 'refund' в вектор-пространстве).
    Хорош для тестов ingest-логики, metadata round-trip, идемпотентности.
    Для смыслового semantic-search используется operator-mode тест с
    настроенной реальной моделью.
    """

    DIM = 64

    def _vec(self, text: str) -> list[float]:
        # SHA-256 (32 bytes) дублируем до 64 — детерминированный пер-байт
        # вектор. Нормализация: переводим [0,255] → [-0.5, 0.5].
        h = hashlib.sha256(text.encode("utf-8")).digest()
        extended = (h * 2)[: self.DIM]
        return [(b / 255.0) - 0.5 for b in extended]

    def embed_documents(
        self,
        contents: Iterable[str],
    ) -> Iterator[Sequence[float]]:
        for c in contents:
            yield self._vec(c)

    def embed_query(self, content: str) -> Sequence[float]:
        return self._vec(content)

    def dim(self) -> int:
        return self.DIM


@pytest.fixture
def fake_embedder() -> Embedder[str]:
    """Детерминированный fake-embedder для контракт-тестов."""
    return _FakeDeterministicEmbedder()


@pytest.fixture
def chromadb_client_persistent(tmp_path: Path) -> Any:
    """Свежий PersistentClient на tmp_path. Cleanup автоматически через tmp_path."""
    import chromadb

    return chromadb.PersistentClient(path=str(tmp_path / "chroma"))


@dataclass(frozen=True)
class OperatorRunSpec:
    """Параметры одного operator-mode прогона ingest.

    `cfg` — реальный `ChromadbPluginConfig`, собранный из env'а и
    `$BOBA_CONFIG_PATH` (по умолчанию `./local/config.toml`). Из cfg
    берутся persist_path и embedding_* параметры.

    `folder` и `collection` — per-run параметры, в plugin-конфиг не
    входят (они меняются от запуска к запуску). Передаются через
    отдельные env-варсы вне chromadb-префикса, чтобы не конфликтовать
    с `extra="forbid"` валидацией cfg.
    """

    cfg: ChromadbPluginConfig
    folder: Path
    collection: CollectionId


@pytest.fixture
def operator_run() -> OperatorRunSpec | None:
    """Operator-mode прогон. None если `cfg.ingest_folder` пуст.

    Все параметры читаются через `ChromadbPluginConfig` — единая система
    конфигурирования (TOML `[tool.chromadb]` + env `BOBA_TOOL__CHROMADB__*`).
    Кастомных env-варсов для теста нет.

    Триггер режима: `cfg.ingest_folder != ""`. Задаётся либо в
    `local/config.toml`:

        [tool.chromadb]
        ingest_folder = "/path/to/kb"
        ingest_collection = "kb"

    либо через env-override:

        BOBA_TOOL__CHROMADB__INGEST_FOLDER=/path/to/kb \\
            pytest .../test_md_folder_ingest.py::test_operator_real_ingest -s

    Поля cfg (persist_path, embedding_*) при этом берутся из того же
    источника — что плагин использует в run-time.
    """
    try:
        cfg = ChromadbPluginConfig()
    except ValidationError:
        return None
    if not cfg.ingest_folder:
        return None
    return OperatorRunSpec(
        cfg=cfg,
        folder=Path(cfg.ingest_folder),
        collection=CollectionId(cfg.ingest_collection),
    )
