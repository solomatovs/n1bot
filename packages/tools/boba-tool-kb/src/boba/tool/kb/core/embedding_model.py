"""`EmbeddingModel` — переиспользуемая базовая модель OpenAI-compat embedder'а.

`BaseModel` (не settings), встраивается как nested-поле в tool-конфиги,
которым нужно строить embeddings (ingest tools для write-side; search
tools для query-side).

Поле `endpoint` (а не `base_url`) — намеренно: при встраивании рядом с
`ConfluenceConnection` (где `base_url` — это URL Confluence) на одном
уровне корневой `BobaFlatSettings`-секции flat-резолв нашёл бы конфликт
двух `base_url`. Хорошо `endpoint` лучше отражает суть — «куда послать
embedding-запрос», независимо от вендора.

Семантический инвариант (НЕ enforced конфигом):
  `model` ДОЛЖНА совпадать между ingest-tools и search-tools, которые
  пишут/читают одну и ту же KB-коллекцию. Иначе ingest положит
  N-мерный вектор, search будет искать M-мерным → silent break.
"""

from __future__ import annotations

from typing import Self

from pydantic import BaseModel, Field, model_validator

__all__ = ["EmbeddingModel"]


class EmbeddingModel(BaseModel):
    """OpenAI-совместимый embedder: endpoint + api_key + model."""

    endpoint: str = Field(
        default="",
        description=(
            "URL OpenAI-совместимого embeddings-endpoint'а (LiteLLM / OpenAI / "
            "vLLM / Ollama). Передаётся как `base_url` при построении `OpenAI` "
            "клиента, но в конфиге названо `endpoint`, чтобы не конфликтовать с "
            "`ConfluenceConnection.base_url` при встраивании в один tool-конфиг."
        ),
    )
    api_key: str = Field(
        default="",
        description=(
            "API-ключ. Для локальных Ollama/vLLM может быть пустым (передаётся "
            "как `unused` в OpenAI-клиент)."
        ),
    )
    model: str = Field(
        default="",
        description=(
            "Имя embedding-модели. Должно совпадать между ingest- и search-"
            "сервисами, ходящими в одни и те же чанки kb_chunks."
        ),
    )

    @model_validator(mode="after")
    def _validate(self) -> Self:
        if not self.endpoint:
            msg = "embedding: endpoint обязателен"
            raise ValueError(msg)
        if not self.model:
            msg = "embedding: model обязателен"
            raise ValueError(msg)
        return self
