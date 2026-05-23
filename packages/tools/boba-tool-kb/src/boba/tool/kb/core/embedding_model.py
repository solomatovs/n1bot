"""`EmbeddingModel` — конфиг embedder'а (OpenAI-compat HTTP или in-process).

`BaseModel` (не settings), встраивается как nested-поле в tool-конфиги,
которым нужно строить embeddings (ingest tools для write-side; search
tools для query-side).

Поле `endpoint` (а не `base_url`) — намеренно: при встраивании рядом с
`ConfluenceConnection` (где `base_url` — это URL Confluence) на одном
уровне корневой `BobaFlatSettings`-секции flat-резолв нашёл бы конфликт
двух `base_url`. Хорошо `endpoint` лучше отражает суть — «куда послать
embedding-запрос», независимо от вендора.

`provider` выбирает бэкенд:
  - "openai-compat" (default) — HTTP в LiteLLM/OpenAI/vLLM/Ollama;
    обязателен `endpoint`, опционально `api_key`.
  - "local" — in-process fastembed (ONNX) без сети; `endpoint`/`api_key`
    игнорируются, `model` — имя HF-модели, опционально `cache_dir` для
    HF-кеша.

Семантический инвариант (НЕ enforced конфигом):
  `model` ДОЛЖНА совпадать между ingest-tools и search-tools, которые
  пишут/читают одну и ту же KB-коллекцию. Иначе ingest положит
  N-мерный вектор, search будет искать M-мерным → silent break.
"""

from __future__ import annotations

from typing import Literal, Self

from pydantic import BaseModel, Field, model_validator

__all__ = ["EmbeddingModel"]


class EmbeddingModel(BaseModel):
    """Embedder-конфиг: HTTP OpenAI-compat либо in-process fastembed."""

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
