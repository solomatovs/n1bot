"""Embedder KB-плагина: конфиг EmbeddingModel + бэкенд.

- LocalFastEmbedEmbedder — in-process Embedder[str] поверх fastembed
  (ONNX runtime, без сети).
- EmbeddingModel         — чистый BaseModel-конфиг (профиль local fastembed),
  встраивается как nested-поле в tool-конфиги.
- LocalFastEmbedEmbedderFactory — собирает LocalFastEmbedEmbedder из
  EmbeddingModel (конфиг отдельно от сборки).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from pydantic import BaseModel, Field

from boba.indexing.embedder import Embedder

__all__ = [
    "EmbeddingModel",
    "LocalFastEmbedEmbedder",
    "LocalFastEmbedEmbedderFactory",
]


class LocalFastEmbedEmbedder(Embedder[str]):
    """Embedder[str] на fastembed TextEmbedding (in-process, ONNX).

    Для асимметричных моделей (e5-family) fastembed сам подставляет нужные
    префиксы: passage_embed -> "passage: ", query_embed -> "query: ".
    Для симметричных моделей оба метода работают как обычный embed.
    """

    def __init__(self, model_name: str, cache_dir: str, dim: int) -> None:
        from fastembed import TextEmbedding  # noqa: PLC0415 — тянет onnxruntime
        self._model_name = model_name
        self._model = TextEmbedding(
            model_name=model_name,
            cache_dir=cache_dir,
        )
        self._dim = dim

    def embed_documents(
        self,
        contents: Iterable[str],
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
    """Профиль embedding: in-process fastembed (ONNX, local, без сети).

    BaseModel, встраивается как nested-поле в tool-конфиги, которым нужно
    строить embeddings (ingest — write-side; search — query-side). В config
    задаётся как профиль [embedding.<name>] и подключается ссылкой
    embedding = "${embedding.<name>}".

    Семантический инвариант (НЕ enforced конфигом): model ДОЛЖНА совпадать
    между ingest-tools и search-tools, которые пишут/читают одну и ту же
    KB-коллекцию. Иначе ingest положит N-мерный вектор, search будет искать
    M-мерным -> silent break.
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
            "Пусто -> дефолт fastembed (~/.cache/fastembed)."
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
        )
