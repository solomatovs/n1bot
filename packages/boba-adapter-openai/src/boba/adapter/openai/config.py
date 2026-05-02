"""Конфиг-секция и LLM-source-фабрика OpenAI-адаптера."""

from __future__ import annotations

from typing import Any, ClassVar

from boba.adapter.openai.terminal import OpenAITerminal, build_openai_client
from boba.domain.config import LLMConfig
from boba.domain.core.config import ConfigSection, FieldSpec, ObjectSchema
from boba.domain.core.validators import ChainConverter, Default, ParseString
from boba.domain.llm.events import LLMEvent
from boba.domain.llm.models import LLMContext
from boba.domain.llm.observer import LLMRequestObserver
from boba.patterns import (
    StreamSource,
    StreamSourceChainBuilder,
    StrId,
)
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
                    Default("http://localhost:4000"), ParseString(),
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
    *,
    reindex_tool_calls: bool = True,
) -> StreamSource[LLMContext, LLMEvent]:
    """Готовый StreamSource поверх openai-SDK с подключённым observer'ом.

    reindex_tool_calls=False отключает починку коллизий index у параллельных
    tool_calls в стриме — для случаев, когда провайдер уже корректен.
    """
    return StreamSourceChainBuilder[LLMContext, LLMEvent]().terminal(
        OpenAITerminal(
            build_openai_client(llm_config, observer),
            observer=observer,
            reindex_tool_calls=reindex_tool_calls,
        )
    )
