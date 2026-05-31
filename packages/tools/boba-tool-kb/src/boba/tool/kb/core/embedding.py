"""Embedder KB-плагина: конфиг `EmbeddingModel` + бэкенды.

- `LocalFastEmbedEmbedder` — in-process `Embedder[str]` поверх fastembed
  (ONNX runtime, без сети).
- `EmbeddingModel`         — `BaseModel`-конфиг (OpenAI-compat HTTP или local),
  встраивается как nested-поле в tool-конфиги. `EmbeddingModel.build()` —
  factory нужного `Embedder[str]` по `provider`.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import ClassVar, Literal, Self

from fastembed import TextEmbedding
from openai import OpenAI
from pydantic import BaseModel, Field, model_validator

from boba.indexing.embedder import Embedder
from boba.provider.openai import OpenAICompatEmbedder

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
    """Embedder-конфиг: HTTP OpenAI-compat либо in-process fastembed.

    `BaseModel` (не settings), встраивается как nested-поле в tool-конфиги,
    которым нужно строить embeddings (ingest tools для write-side; search
    tools для query-side).

    Поле `endpoint` (а не `base_url`) — намеренно: при встраивании рядом с
    `ConfluenceConnection` (где `base_url` — это URL Confluence) на одном
    уровне корневой `BobaFlatSettings`-секции flat-резолв нашёл бы конфликт
    двух `base_url`.

    Семантический инвариант (НЕ enforced конфигом): `model` ДОЛЖНА совпадать
    между ingest-tools и search-tools, которые пишут/читают одну и ту же
    KB-коллекцию. Иначе ingest положит N-мерный вектор, search будет искать
    M-мерным → silent break.
    """

    provider: Literal["openai-compat", "local"] = Field(
        default="openai-compat",
        description=(
            "Бэкенд: 'openai-compat' — HTTP в LiteLLM/OpenAI/vLLM/Ollama; "
            "'local' — in-process fastembed (ONNX) без сети."
        ),
    )
    endpoint: str = Field(
        default="",
        description=(
            "URL OpenAI-совместимого embeddings-endpoint'а (LiteLLM / OpenAI / "
            "vLLM / Ollama). Обязателен для provider='openai-compat'. Для "
            "provider='local' игнорируется."
        ),
    )
    api_key: str = Field(
        default="",
        description=(
            "API-ключ. Для локальных Ollama/vLLM может быть пустым (передаётся "
            "как `unused` в OpenAI-клиент). Для provider='local' игнорируется."
        ),
    )
    model: str = Field(
        default="",
        description=(
            "Имя embedding-модели. Должно совпадать между ingest- и search-"
            "сервисами, ходящими в одни и те же чанки kb_chunks."
        ),
    )
    cache_dir: str = Field(
        default="",
        description=(
            "Только для provider='local': каталог для HF/ONNX-весов fastembed. "
            "Пусто → дефолт fastembed (~/.cache/fastembed)."
        ),
    )

    @model_validator(mode="after")
    def _validate(self) -> Self:
        if not self.model:
            msg = "embedding: model обязателен"
            raise ValueError(msg)
        if self.provider == "openai-compat" and not self.endpoint:
            msg = "embedding: endpoint обязателен для provider='openai-compat'"
            raise ValueError(msg)
        return self

    def build(self) -> Embedder[str]:
        """Factory `Embedder[str]` по `provider`.

        `openai-compat` — `OpenAICompatEmbedder` поверх OpenAI-SDK (без
        `dimensions=`, размерность — lazy probe в `dim()`); `local` —
        `LocalFastEmbedEmbedder` (fastembed/ONNX, in-process, без сети).
        """
        if self.provider == "local":
            return LocalFastEmbedEmbedder(
                model_name=self.model,
                cache_dir=self.cache_dir or None,
            )

        client = OpenAI(
            base_url=self.endpoint or None,
            api_key=self.api_key or "unused",
        )
        return OpenAICompatEmbedder(
            client=client,
            model=self.model,
        )
