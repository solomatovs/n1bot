"""Embedder KB-плагина: конфиг EmbeddingModel, бэкенд fastembed и его фабрика."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from typing import ClassVar

from pydantic import BaseModel, Field

from boba.indexing.ports import Embedder
from boba.toolkit.cpu import CpuBudget

logger = logging.getLogger(__name__)

__all__ = [
    "EmbeddingModel",
    "LocalFastEmbedEmbedder",
    "LocalFastEmbedEmbedderFactory",
]


class LocalFastEmbedEmbedder(Embedder[str]):
    """Embedder[str] на fastembed (in-process, ONNX); префиксы e5 подставляет fastembed.

    Модель одна на процесс, поэтому инференс уходит в поток под локом: loop
    остаётся свободен для остальных источников, а ONNX внутри и так занимает
    все выделенные ядра — параллельные прогоны только отнимали бы память.
    """

    def __init__(
        self, model_name: str, cache_dir: str, dim: int, batch_size: int
    ) -> None:
        from fastembed import (  # noqa: PLC0415 # pyright: ignore[reportMissingImports]
            TextEmbedding,
        )

        # onnxruntime считает ядра по хосту и cgroup-квоту не видит: без
        # threads его пул дерётся сам с собой (на одном ядре — в 5 раз дольше)
        threads = CpuBudget.cores()
        logger.info("embedder: %s on %d threads", model_name, threads)

        self._model_name = model_name
        self._model = TextEmbedding(
            model_name=model_name,
            cache_dir=cache_dir,
            threads=threads,
        )
        self._dim = dim
        self._batch_size = batch_size
        self._lock = asyncio.Lock()

    async def embed_documents(
        self,
        contents: Sequence[str],
    ) -> Sequence[Sequence[float]]:
        async with self._lock:
            return await asyncio.to_thread(self._passage_embed, contents)

    async def embed_query(self, content: str) -> Sequence[float]:
        async with self._lock:
            return await asyncio.to_thread(self._query_embed, content)

    PROGRESS_EVERY: ClassVar[int] = 25
    """Шаг прогресса в журнале: длинный батч не должен выглядеть зависшим."""

    def _passage_embed(self, contents: Sequence[str]) -> list[Sequence[float]]:
        vectors: list[Sequence[float]] = []
        for vec in self._model.passage_embed(contents, batch_size=self._batch_size):
            v = vec.tolist()
            self._record_dim(v)
            vectors.append(v)

            if len(vectors) % self.PROGRESS_EVERY == 0:
                logger.info(
                    "embedding progress: %d/%d", len(vectors), len(contents)
                )

        return vectors

    def _query_embed(self, content: str) -> Sequence[float]:
        gen = self._model.query_embed([content], batch_size=self._batch_size)
        vec = next(iter(gen))
        v = vec.tolist()
        self._record_dim(v)
        return v

    def dim(self) -> int:
        return self._dim

    def _record_dim(self, vec: Sequence[float]) -> None:
        actual = len(vec)
        if actual != self._dim:
            msg = (
                f"LocalFastEmbedEmbedder: model {self._model_name!r} returned "
                f"vectors of dim {actual}, config declares {self._dim}"
            )
            raise RuntimeError(msg)


class EmbeddingModel(BaseModel):
    """Профиль embedding (in-process fastembed): model ДОЛЖНА совпадать между
    ingest- и search-tools одной KB-коллекции, иначе silent break размерностей."""

    model: str = Field(
        description=(
            "Имя fastembed-модели (HF). Обязательно. Должно совпадать между "
            "ingest- и search-сервисами, ходящими в одни и те же чанки kb_chunks."
        ),
    )
    cache_dir: str = Field(
        default="",
        description=(
            "Каталог для HF/ONNX-весов fastembed. "
            "Пусто -> дефолт fastembed (~/.cache/fastembed)."
        ),
    )
    batch_size: int = Field(
        gt=0,
        description=(
            "Сколько текстов уходит в модель за один прогон; обязателен. "
            "Активации ONNX растут линейно по батчу: на e5-large батч 100 "
            "занимал 6.5G и ловил OOM, батч 8 — около 2.3G."
        ),
    )
    dim: int = Field(
        gt=0,
        description=(
            "Размерность вектора модели; обязательна. Задаётся явно, чтобы "
            "процесс приложения не грузил модель ради одного числа — веса "
            "лежат только внутри песочницы. Расхождение с фактическим "
            "вектором ловит LocalFastEmbedEmbedder на первом же эмбеддинге."
        ),
    )


class LocalFastEmbedEmbedderFactory:
    """Собирает LocalFastEmbedEmbedder из конфига EmbeddingModel."""

    @staticmethod
    def build(cfg: EmbeddingModel) -> LocalFastEmbedEmbedder:
        return LocalFastEmbedEmbedder(
            model_name=cfg.model,
            cache_dir=cfg.cache_dir,
            dim=cfg.dim,
            batch_size=cfg.batch_size,
        )
