"""Размер порции для модели: инференс ONNX упирается в память именно здесь.

Замеры на e5-large внутри песочницы (VmRSS после загрузки модели — 1.5G):
батч 4 — 1.9G, батч 8 — 2.3G, батч 100 — 6.5G и OOM при лимите группы 6G.
Батчирование в postgres (diff/upsert) живёт отдельно и остаётся крупным —
эти два размера нельзя путать.
"""

from __future__ import annotations

import sys
import types
from typing import Any, ClassVar

import pytest

from boba.tool.kb.embedding import EmbeddingModel, LocalFastEmbedEmbedderFactory

MAX_REASONABLE_BATCH = 16
"""Выше этого инференс e5-large перестаёт помещаться в лимит профиля kb."""


class FakeTextEmbedding:
    """Заглушка fastembed: запоминает, с каким batch_size её позвали."""

    calls: ClassVar[list[dict[str, Any]]] = []

    def __init__(self, model_name: str, cache_dir: str) -> None:
        self.model_name = model_name
        self.cache_dir = cache_dir

    def passage_embed(self, texts, **kwargs):
        FakeTextEmbedding.calls.append({"method": "passage", **kwargs})
        for _ in texts:
            yield _Vector([0.0] * 384)

    def query_embed(self, texts, **kwargs):
        FakeTextEmbedding.calls.append({"method": "query", **kwargs})
        for _ in texts:
            yield _Vector([0.0] * 384)


class _Vector(list):
    def tolist(self) -> list[float]:
        return list(self)


@pytest.fixture
def fake_fastembed(monkeypatch: pytest.MonkeyPatch):
    FakeTextEmbedding.calls = []
    module = types.ModuleType("fastembed")
    module.TextEmbedding = FakeTextEmbedding
    monkeypatch.setitem(sys.modules, "fastembed", module)
    return FakeTextEmbedding


def _config(batch_size: int) -> EmbeddingModel:
    return EmbeddingModel(
        model="intfloat/multilingual-e5-small",
        cache_dir="/opt/fastembed",
        dim=384,
        batch_size=batch_size,
    )


class TestBatchSizeReachesModel:
    """Настройка обязана доезжать до fastembed, иначе он берёт свой дефолт."""

    def test_documents_are_embedded_in_configured_batches(self, fake_fastembed) -> None:
        embedder = LocalFastEmbedEmbedderFactory.build(_config(8))
        list(embedder.embed_documents([f"текст {i}" for i in range(100)]))
        assert fake_fastembed.calls == [{"method": "passage", "batch_size": 8}]

    def test_query_uses_the_same_batch_size(self, fake_fastembed) -> None:
        embedder = LocalFastEmbedEmbedderFactory.build(_config(8))
        embedder.embed_query("запрос")
        assert fake_fastembed.calls == [{"method": "query", "batch_size": 8}]


class TestConfigKeepsBatchSmall:
    """Конфиг приложения: батч модели должен оставаться в пределах памяти."""

    @pytest.mark.parametrize("section", ["tool.kb", "tool.ingest"])
    def test_batch_size_is_bounded(self, raw_config, section: str) -> None:
        from boba.settings import bind

        embedding = bind(raw_config, path=f"{section}.embedding", model=EmbeddingModel)
        assert 0 < embedding.batch_size <= MAX_REASONABLE_BATCH, (
            f"[{section}.embedding]: batch_size={embedding.batch_size} — "
            "инференс ONNX растёт линейно по батчу и словит OOM"
        )
