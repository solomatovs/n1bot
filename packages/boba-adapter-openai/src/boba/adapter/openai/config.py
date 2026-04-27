"""Конфиг-секция и LLM-source-фабрика OpenAI-адаптера.

Секция мапит env/TOML-ключи в LLMConfig
(base_url + api_key) — формат запроса OpenAI-совместимый,
поэтому годится и для LiteLLM/Ollama/прочих прокси.

create_llm_source — фабрика готового
StreamSource[LLMContext, LLMEvent]: оборачивает
OpenAITerminal в StreamSourceChainBuilder. Bootstrap
приложения вызывает её с app_config.llm (DTO собран
LLMTransportSection) и LLMRequestObserver под OpenAI-типы.
"""

from __future__ import annotations

from typing import Any, ClassVar

from boba.adapter.openai.terminal import OpenAITerminal, build_openai_client
from boba.domain.config import LLMConfig
from boba.domain.core.config import ConfigSection, FieldSpec, ObjectSchema
from boba.domain.core.patterns import (
    StreamSource,
    StreamSourceChainBuilder,
    StrId,
)
from boba.domain.core.validators import ChainConverter, Default, ParseString
from boba.domain.llm.events import LLMEvent
from boba.domain.llm.models import LLMContext
from boba.domain.llm.observer import LLMRequestObserver
from openai.types.chat.chat_completion_chunk import ChatCompletionChunk


class LLMTransportSection(ConfigSection[LLMConfig]):
    """Транспорт LLM-клиента: base_url + api_key."""

    id: ClassVar[StrId] = StrId("llm_transport")
    namespace: ClassVar[tuple[str, ...]] = ("llm",)

    schema: ClassVar[ObjectSchema[LLMConfig]] = ObjectSchema(
        description="Транспорт LLM-клиента: base_url + api_key.",
        fields=[
            FieldSpec(
                name="base_url",
                converter=ChainConverter(
                    Default("http://localhost:11434/v1"), ParseString(),
                ),
                description="OpenAI-совместимый base URL LLM-сервера "
                "(LiteLLM/Ollama/...).",
            ),
            FieldSpec(
                name="api_key",
                converter=ChainConverter(Default("ollama"), ParseString()),
                description="API-ключ LLM-сервера. "
                "Для локального Ollama — любой непустой.",
            ),
        ],
        factory=LLMConfig,
    )


def create_llm_source(
    llm_config: LLMConfig,
    observer: LLMRequestObserver[dict[str, Any], ChatCompletionChunk],
) -> StreamSource[LLMContext, LLMEvent]:
    """Готовый StreamSource поверх openai-SDK с подключённым observer'ом."""
    return StreamSourceChainBuilder[LLMContext, LLMEvent]().terminal(
        OpenAITerminal(
            build_openai_client(llm_config),
            observer=observer,
        )
    )
