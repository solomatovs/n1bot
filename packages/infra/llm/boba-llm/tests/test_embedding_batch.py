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

from boba.llm.embedding import EmbedderFactory, LocalEmbedding
from boba.toolkit.cpu import CpuBudget


class FakeTextEmbedding:
    """Заглушка fastembed: запоминает batch_size и число потоков движка."""

    calls: ClassVar[list[dict[str, Any]]] = []
    threads: ClassVar[int | None] = None

    def __init__(self, model_name: str, cache_dir: str, threads: int | None) -> None:
        self.model_name = model_name
        self.cache_dir = cache_dir
        FakeTextEmbedding.threads = threads

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


def _config(batch_size: int) -> LocalEmbedding:
    return LocalEmbedding(
        provider="local",
        model="intfloat/multilingual-e5-small",
        cache_dir="/var/cache/fastembed",
        dim=384,
        batch_size=batch_size,
        progress_every=1,
    )


class TestBatchSizeReachesModel:
    """Настройка обязана доезжать до fastembed, иначе он берёт свой дефолт."""

    @pytest.mark.anyio
    async def test_documents_are_embedded_in_configured_batches(
        self,
        fake_fastembed,
    ) -> None:
        embedder = EmbedderFactory.build(_config(8))
        await embedder.embed_documents([f"текст {i}" for i in range(100)])
        if fake_fastembed.calls != [{"method": "passage", "batch_size": 8}]:
            raise AssertionError('fake_fastembed.calls == [{"method": "passage", "bat…')

    @pytest.mark.anyio
    async def test_query_uses_the_same_batch_size(self, fake_fastembed) -> None:
        embedder = EmbedderFactory.build(_config(8))
        await embedder.embed_query("запрос")
        if fake_fastembed.calls != [{"method": "query", "batch_size": 8}]:
            raise AssertionError('fake_fastembed.calls == [{"method": "query", "batch…')


class TestThreadsFollowTheQuota:
    """Потоки движка — по квоте запуска: иначе onnxruntime берёт ядра хоста
    и под cgroup-квотой его пул дерётся сам с собой (замер: ×5 на одном ядре).
    """

    @pytest.mark.parametrize(("cores", "expected"), [("1", 1), ("4", 4)])
    def test_threads_come_from_the_cpu_budget(
        self,
        fake_fastembed,
        monkeypatch: pytest.MonkeyPatch,
        cores: str,
        expected: int,
    ) -> None:
        monkeypatch.setenv(CpuBudget.ENV_VAR, cores)

        EmbedderFactory.build(_config(8))

        if fake_fastembed.threads != expected:
            raise AssertionError("fake_fastembed.threads == expected")
