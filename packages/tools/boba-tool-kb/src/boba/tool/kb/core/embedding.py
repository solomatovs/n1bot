"""Embedder KB-плагина: конфиг `EmbeddingModel` + бэкенд.

- `LocalFastEmbedEmbedder` — in-process `Embedder[str]` поверх fastembed
  (ONNX runtime, без сети).
- `EmbeddingModel`         — `BaseModel`-конфиг (профиль local fastembed),
  встраивается как nested-поле в tool-конфиги. `EmbeddingModel.build()` —
  factory `Embedder[str]`.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import ClassVar

from fastembed import TextEmbedding
from pydantic import BaseModel, Field

from boba.indexing.embedder import Embedder

__all__ = ["EmbeddingModel", "LocalFastEmbedEmbedder"]


class LocalFastEmbedEmbedder(Embedder[str]):
    """`Embedder[str]` на fastembed TextEmbedding (in-process, ONNX).

    Для асимметричных моделей (e5-family) fastembed сам подставляет нужные
    префиксы: `passage_embed` → "passage: ", `query_embed` → "query: ".
    Для симметричных моделей оба метода работают как обычный embed.
    """

    _PROBE_INPUT: ClassVar[str] = "dim-probe"

    def __init__(self, model_name: str, cache_dir: str | None = None) -> None:
        self._model_name = model_name
        self._model = TextEmbedding(
            model_name=model_name,
            cache_dir=cache_dir,
        )
        self._dim: int | None = None

    def embed_documents(
        self, contents: Iterable[str],
    ) -> Iterable[Sequence[float]]:
        for vec in self._model.passage_embed(contents):
            v = vec.tolist()
            self._record_dim(v)
            yield v

    def embed_query(self, content: str) -> Sequence[float]:
        gen = self._model.query_embed([content])
        vec = next(iter(gen))
        v = vec.tolist()
        self._record_dim(v)
        return v

    def dim(self) -> int:
        if self._dim is None:
            vec = self.embed_query(self._PROBE_INPUT)
            if not vec:
                msg = (
                    f"LocalFastEmbedEmbedder: empty embedding from "
                    f"model {self._model_name!r} on probe input"
                )
                raise RuntimeError(msg)
            self._dim = len(vec)
        return self._dim

    def _record_dim(self, vec: Sequence[float]) -> None:
        actual = len(vec)
        if self._dim is None:
            self._dim = actual
        elif actual != self._dim:
            msg = (
                f"LocalFastEmbedEmbedder: model {self._model_name!r} returned "
                f"vectors of inconsistent dim — first {self._dim}, now {actual}"
            )
            raise RuntimeError(msg)


class EmbeddingModel(BaseModel):
    """Профиль embedding: in-process fastembed (ONNX, local, без сети).

    `BaseModel`, встраивается как nested-поле в tool-конфиги, которым нужно
    строить embeddings (ingest — write-side; search — query-side). В config
    задаётся как профиль `[embedding.<name>]` и подключается ссылкой
    `embedding = "${embedding.<name>}"`.

    Семантический инвариант (НЕ enforced конфигом): `model` ДОЛЖНА совпадать
    между ingest-tools и search-tools, которые пишут/читают одну и ту же
    KB-коллекцию. Иначе ingest положит N-мерный вектор, search будет искать
    M-мерным → silent break.
    """

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
            "Пусто → дефолт fastembed (~/.cache/fastembed)."
        ),
    )

    def build(self) -> Embedder[str]:
        """Factory `LocalFastEmbedEmbedder` (fastembed/ONNX, in-process)."""
        return LocalFastEmbedEmbedder(
            model_name=self.model,
            cache_dir=self.cache_dir or None,
        )
