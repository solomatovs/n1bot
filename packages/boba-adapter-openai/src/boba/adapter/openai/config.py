"""Конфиг-секция и LLM-source-фабрика OpenAI-адаптера."""

from __future__ import annotations

from typing import Any, ClassVar

from boba.config import ConfigSection
from boba.declaration import FieldSpec, ObjectSchema
from boba.llm.events import LLMEvent
from boba.llm.models import LLMContext
from boba.llm.observer import LLMRequestObserver
from boba.validators import ChainConverter, Default, ParseString

from boba.adapter.openai.dto import OpenAIConfig
from boba.adapter.openai.terminal import OpenAITerminal, build_openai_client
from boba.patterns import (
    StreamSource,
    StreamSourceChainBuilder,
    StrId,
)
from openai.types.chat.chat_completion_chunk import ChatCompletionChunk


class LLMTransportSection(ConfigSection[OpenAIConfig]):
    """Транспорт LLM-клиента: base_url + api_key."""

    id: ClassVar[StrId] = StrId("llm_transport")
    namespace: ClassVar[tuple[str, ...]] = ("llm",)

    schema: ClassVar[ObjectSchema[OpenAIConfig]] = ObjectSchema(
        description="Транспорт LLM-клиента: base_url + api_key.",
        fields=[
            FieldSpec(
                name="base_url",
                converter=ChainConverter(
                    Default("http://localhost:4000"),
                    ParseString(),
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
        factory=OpenAIConfig,
    )


def create_llm_source(
    llm_config: OpenAIConfig,
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
