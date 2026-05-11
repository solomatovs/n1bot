"""DTO OpenAI-адаптера: OpenAIConfig (транспорт)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from boba.schema.coercion import ParseString

__all__ = ["OpenAIConfig"]


@dataclass(frozen=True)
class OpenAIConfig:
    """OpenAI-совместимый LLM-адаптер: base_url + api_key."""

    base_url: Annotated[
        str,
        "OpenAI-совместимый base URL LLM-сервера (LiteLLM/Ollama/...).",
        ParseString(),
    ] = "http://localhost:4000"
    api_key: Annotated[
        str,
        "API-ключ LLM-сервера. Для локального Ollama — любой непустой.",
        ParseString(),
    ] = "ollama"
