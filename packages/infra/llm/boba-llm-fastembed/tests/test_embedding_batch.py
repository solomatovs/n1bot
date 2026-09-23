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

from boba.llm.fastembed import FastembedBackend, FastembedProvider
from boba.llm.providers import EmbeddingModelConfig

pytestmark = pytest.mark.anyio


@pytest.fixture(scope="module")
def anyio_backend() -> str:
    return "asyncio"


class FakeTextEmbedding:
    """Заглушка fastembed: запоминает batch_size."""

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


class _FastembedModule(types.ModuleType):
    """Модуль-заглушка: атрибут объявлен типом, а не проставлен снаружи."""

    TextEmbedding: ClassVar[type[FakeTextEmbedding]] = FakeTextEmbedding


@pytest.fixture
def fake_fastembed(monkeypatch: pytest.MonkeyPatch):
    FakeTextEmbedding.calls = []
    monkeypatch.setitem(sys.modules, "fastembed", _FastembedModule("fastembed"))
    return FakeTextEmbedding


PROVIDER = FastembedProvider(kind="fastembed", cache_dir="/var/cache/fastembed")


def _config(batch_size: int) -> EmbeddingModelConfig:
    return EmbeddingModelConfig(
        provider=PROVIDER,
        model="intfloat/multilingual-e5-small",
        dim=384,
        batch_size=batch_size,
        progress_every=1,
    )


class TestBatchSizeReachesModel:
    """Настройка обязана доезжать до fastembed, иначе он берёт свой дефолт."""

    async def test_documents_are_embedded_in_configured_batches(
        self,
        fake_fastembed,
    ) -> None:
        embedder = FastembedBackend(PROVIDER).embedding(_config(8))
        await embedder.embed_documents([f"текст {i}" for i in range(100)])

        passage = [c for c in fake_fastembed.calls if c["method"] == "passage"]
        if passage != [{"method": "passage", "batch_size": 8}]:
            raise AssertionError(f"batch_size не доехал: {passage}")

    async def test_query_uses_query_prefix_path(self, fake_fastembed) -> None:
        embedder = FastembedBackend(PROVIDER).embedding(_config(8))
        vector = await embedder.embed_query("вопрос")

        if len(vector) != 384:
            raise AssertionError(len(vector))
        if fake_fastembed.calls[-1]["method"] != "query":
            raise AssertionError(fake_fastembed.calls)


class TestWarmModels:
    """Равный конфиг использования отдаёт уже загруженную модель."""

    async def test_same_config_shares_the_loaded_model(self, fake_fastembed) -> None:
        backend = FastembedBackend(PROVIDER)

        first = backend.embedding(_config(8))
        second = backend.embedding(_config(8))
        other = backend.embedding(_config(4))

        if first is not second:
            raise AssertionError("одинаковый конфиг — один экземпляр")
        if other is first:
            raise AssertionError("другой батч — другая модель")
