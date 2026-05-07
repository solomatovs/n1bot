"""DTO OpenAI-адаптера: OpenAIConfig (транспорт) + SCHEMA."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from boba.coercion import ChainCoercer, Default, ParseString
from boba.declaration import FieldSpec, ObjectSchema

__all__ = ["OpenAIConfig"]


@dataclass(frozen=True)
class OpenAIConfig:
    """Транспорт OpenAI-совместимого LLM-клиента (base_url + api_key)."""

    base_url: str = "http://localhost:11434/v1"
    api_key: str = "ollama"

    SCHEMA: ClassVar[ObjectSchema[OpenAIConfig]]


OpenAIConfig.SCHEMA = ObjectSchema(
    description="OpenAI-совместимый LLM-адаптер: base_url + api_key.",
    fields=[
        FieldSpec(
            name="base_url",
            coercer=ChainCoercer(
                Default("http://localhost:4000"),
                ParseString(),
            ),
            description=(
                "OpenAI-совместимый base URL LLM-сервера (LiteLLM/Ollama/...)."
            ),
        ),
        FieldSpec(
            name="api_key",
            coercer=ChainCoercer(Default("ollama"), ParseString()),
            description=(
                "API-ключ LLM-сервера. Для локального Ollama — любой непустой."
            ),
        ),
    ],
    factory=OpenAIConfig,
)
