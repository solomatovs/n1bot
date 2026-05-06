"""DTO OpenAI-адаптера: OpenAIConfig (транспорт)."""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["OpenAIConfig"]


@dataclass(frozen=True)
class OpenAIConfig:
    """Транспорт OpenAI-совместимого LLM-клиента (base_url + api_key)."""

    base_url: str = "http://localhost:11434/v1"
    api_key: str = "ollama"
