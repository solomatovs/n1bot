"""`EmbeddingConfig` — подключение к embedding-провайдеру.

Секция `[tool.kb.embedding]`. Структурно идентично
`[tool.kb.postgres]` / `[tool.kb.confluence]`: connection-параметры
живут отдельным конфигом, который через FromConfig получает
`provide_embedder` (см. `providers.py`).

Поддерживаются любые OpenAI-совместимые endpoint'ы (LiteLLM /
OpenAI / vLLM / Ollama). Размерность вектора определяется lazy
probe'ом в `OpenAICompatEmbedder.dim()` — её в конфиге нет.
"""

from __future__ import annotations

from typing import Self

from pydantic import Field, model_validator

from boba.settings import BobaFlatSettings, BobaSettingsConfigDict

__all__ = ["EmbeddingConfig"]


class EmbeddingConfig(BobaFlatSettings):
    """Embedding connection-config: общий для ingest и read-side путей."""

    model_config = BobaSettingsConfigDict(
        case_sensitive=False,
        extra="ignore",
        config_path="tool.kb.embedding",
    )

    model: str = Field(
        default="",
        description=(
            "Имя embedding-модели на OpenAI-совместимом endpoint'е. "
            "Обязателен. Размерность вектора детектится автоматически "
            "по первому ответу модели (`OpenAICompatEmbedder.dim()`)."
        ),
    )
    base_url: str = Field(
        default="",
        description=(
            "OpenAI-совместимый endpoint embeddings "
            "(LiteLLM / OpenAI / vLLM / Ollama). Пусто → дефолт OpenAI SDK."
        ),
    )
    api_key: str = Field(
        default="",
        description=(
            "API key endpoint'а. Обычно из env "
            "`BOBA_TOOL__KB__EMBEDDING__API_KEY`. Для локального "
            "Ollama/vLLM можно оставить пустым."
        ),
    )

    @model_validator(mode="after")
    def _validate(self) -> Self:
        """Под discover-flow конфиг грузится только если плагин enabled.

        Значит к load-time `model` должен быть заполнен (TOML или env),
        иначе ingest и kb_search будут падать в рантайме на пустой строке.
        """
        if not self.model:
            msg = (
                "kb.embedding.model обязателен (задайте "
                "[tool.kb.embedding].model или "
                "BOBA_TOOL__KB__EMBEDDING__MODEL)"
            )
            raise ValueError(msg)
        return self
