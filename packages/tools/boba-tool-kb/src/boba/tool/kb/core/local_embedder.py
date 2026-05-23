"""In-process `Embedder[str]` поверх fastembed (ONNX runtime, без сети).

Для асимметричных моделей (e5-family) fastembed сам подставляет нужные
префиксы: `passage_embed` → "passage: ", `query_embed` → "query: ".
Для симметричных моделей оба метода работают как обычный embed.

Потоковая обработка: fastembed принимает `Iterable[str]` и возвращает
генератор numpy-векторов, поэтому мы yield'им через — без аккумуляции
документов в память (см. feedback_streaming).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from fastembed import TextEmbedding

from boba.indexing.embedder import Embedder

__all__ = ["LocalFastEmbedEmbedder"]


class LocalFastEmbedEmbedder(Embedder[str]):
    """`Embedder[str]` на fastembed TextEmbedding (in-process, ONNX)."""

    _PROBE_INPUT = "dim-probe"

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
