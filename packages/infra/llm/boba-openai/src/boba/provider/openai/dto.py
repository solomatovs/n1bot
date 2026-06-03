"""DTO OpenAI-адаптера: OpenAIConfig (транспорт)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

__all__ = ["OpenAIConfig"]


class OpenAIConfig(BaseModel):
    """OpenAI-совместимый LLM-адаптер: base_url + api_key."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    base_url: str = Field(
        default="http://localhost:4000",
        description="OpenAI-совместимый base URL LLM-сервера (LiteLLM/Ollama/...).",
    )
    api_key: str = Field(
        default="ollama",
        description="API-ключ LLM-сервера. Для локального Ollama — любой непустой.",
    )
    ssl_verify: bool = Field(
        default=False,
        description="Проверять TLS-сертификат LLM-сервера.",
    )
    tool_calls_fallback: bool = Field(
        default=False,
        description=(
            "Опциональный fallback: если провайдер вернул tool-call текстом в "
            "content (а не в нативном tool_calls), распарсить его и перемапить "
            "в tool_calls итогового сообщения. Чинит конкретную ошибку "
            "провайдера; по умолчанию выкл."
        ),
    )
