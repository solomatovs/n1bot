"""Конфиг-секция и LLM-source-фабрика OpenAI-адаптера.

Секция мапит env/TOML-ключи в :class:`boba.domain.config.LLMConfig`
(``base_url`` + ``api_key``) — формат запроса OpenAI-совместимый,
поэтому годится и для LiteLLM/Ollama/прочих прокси.

:func:`create_llm_source` — фабрика готового
:class:`StreamSource[LLMContext, LLMEvent]`: оборачивает
:class:`OpenAITerminal` в :class:`StreamSourceChainBuilder`. Bootstrap
приложения вызывает её с ``app_config.llm`` (DTO собран
:class:`LLMTransportSection`) и ``RawLLMObserver`` нужного типа.
"""

from __future__ import annotations

from typing import ClassVar

from boba.adapter.openai.raw_observer import RawLLMObserver
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


class LLMTransportSection(ConfigSection[LLMConfig]):
    """Транспорт LLM-клиента: ``base_url`` + ``api_key``."""

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
    observer: RawLLMObserver,
) -> StreamSource[LLMContext, LLMEvent]:
    """Готовый :class:`StreamSource` поверх openai-SDK с подключённым observer'ом."""
    return StreamSourceChainBuilder[LLMContext, LLMEvent]().terminal(
        OpenAITerminal(
            build_openai_client(llm_config),
            observer=observer,
        )
    )
