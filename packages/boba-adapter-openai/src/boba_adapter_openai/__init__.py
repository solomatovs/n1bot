"""OpenAI-совместимый LLM-адаптер для Boba.

Реализует :class:`StreamSource[LLMContext, LLMEvent]` поверх openai-SDK
(годен и для LiteLLM-прокси, Ollama и других openai-совместимых
бэкендов). Внутри:

- :class:`OpenAITerminal` — терминал LLM-цепочки (отправляет HTTP,
  стримит chunks, конвертирует в :class:`LLMEvent`);
- :class:`build_openai_client` — фабрика SDK-клиента из
  :class:`LLMConfig`;
- :class:`RawLLMObserver` + реализации (file-, content-, composite-,
  metrics-) — наблюдатели сырых kwargs/chunks для отладки и сбора
  датасетов;
- :class:`DuplicateToolCallIndexReindexer` — починка кривых
  ``index``-полей в delta-чанках от провайдеров, которые повторяют
  индексы между разными tool_call'ами в одном round-trip.

Пакет — отдельный pip-package; основной ``boba`` от него НЕ зависит.
Подключение через bootstrap (container.py / session.py / CLI):

    from boba_adapter_openai import OpenAITerminal, build_openai_client
"""

from boba_adapter_openai.config import LLMTransportSection, create_llm_source
from boba_adapter_openai.raw_observer import (
    CompositeRawLLMObserver,
    FileContentObserver,
    FileRawLLMObserver,
    MetricsRawLLMObserver,
    RawLLMObserver,
    RequestOutcome,
)
from boba_adapter_openai.raw_observer import (
    MultiKeyReasoningExtractor as RawObserverReasoningExtractor,
)
from boba_adapter_openai.terminal import OpenAITerminal, build_openai_client
from boba_adapter_openai.tool_call_reindexer import (
    DuplicateToolCallIndexReindexer,
)

__all__ = [
    "CompositeRawLLMObserver",
    "DuplicateToolCallIndexReindexer",
    "FileContentObserver",
    "FileRawLLMObserver",
    "LLMTransportSection",
    "MetricsRawLLMObserver",
    "OpenAITerminal",
    "RawLLMObserver",
    "RawObserverReasoningExtractor",
    "RequestOutcome",
    "build_openai_client",
    "create_llm_source",
]
