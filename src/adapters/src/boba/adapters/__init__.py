"""Публичный API adapters-слоя.

Реэкспортирует фасадные адаптеры: sink'и, сервисы workspace/history/
messages, OpenAI-совместимый клиент LLM, провайдеры промптов и
инструментов. Внутренние классы конкретных файлов (``IsServerError``,
``RoleSource``, ``ThinkingSource`` и т.п. из :mod:`.openai_completion`)
здесь не выставляются — это детали реализации, импортируй их напрямую
из сабмодуля при необходимости.

Импорты короткой формой::

    from boba.adapters import OpenAIMiddleware, StupidRetryLLMMiddleware
    from boba.adapters import JsonLinesHistoryService, ConsoleSink, HistorySink
    from boba.adapters import FsWorkspaceManager, FsWorkspaceService
"""

from boba.adapters.aggregating_llm_request_factory import AggregatingLLMRequestFactory
from boba.adapters.console_sink import ConsoleSink
from boba.adapters.fs_workspace import (
    FsPathValidator,
    FsWorkspaceManager,
    FsWorkspaceService,
)
from boba.adapters.history_sink import HistorySink
from boba.adapters.in_memory_messages import InMemoryMessageService
from boba.adapters.jsonl_history import (
    HistoryEntryDecoder,
    HistoryEntryEncoder,
    JsonLinesHistoryService,
)
from boba.adapters.openai_completion import (
    FromOpenAIChunkConverter,
    OpenAIErrorConverter,
    OpenAIMiddleware,
    StupidRetryLLMMiddleware,
    ToOpenAIMessageConverter,
    ToOpenAIOneMessageConverter,
    ToOpenAIRequestConverter,
    ToOpenAIToolConverter,
)
from boba.adapters.prompt_providers import (
    EnvironmentPromptProvider,
    FilePromptProvider,
    GitPromptProvider,
    IDESelectionProvider,
    StaticPromptProvider,
    TemplateProvider,
    UserQueryProvider,
    WorkspaceSystemPromptProvider,
)
from boba.adapters.raw_llm_observer import (
    CompositeRawLLMObserver,
    FileContentObserver,
    FileRawLLMObserver,
    MetricsRawLLMObserver,
    MultiKeyReasoningExtractor,
    RawLLMObserver,
)
from boba.adapters.tool_providers import StaticToolSource

__all__ = [
    "AggregatingLLMRequestFactory",
    "CompositeRawLLMObserver",
    "ConsoleSink",
    "EnvironmentPromptProvider",
    "FileContentObserver",
    "FilePromptProvider",
    "FileRawLLMObserver",
    "FromOpenAIChunkConverter",
    "FsPathValidator",
    "FsWorkspaceManager",
    "FsWorkspaceService",
    "GitPromptProvider",
    "HistoryEntryDecoder",
    "HistoryEntryEncoder",
    "HistorySink",
    "IDESelectionProvider",
    "InMemoryMessageService",
    "JsonLinesHistoryService",
    "MetricsRawLLMObserver",
    "MultiKeyReasoningExtractor",
    "OpenAIErrorConverter",
    "OpenAIMiddleware",
    "RawLLMObserver",
    "StaticPromptProvider",
    "StaticToolSource",
    "StupidRetryLLMMiddleware",
    "TemplateProvider",
    "ToOpenAIMessageConverter",
    "ToOpenAIOneMessageConverter",
    "ToOpenAIRequestConverter",
    "ToOpenAIToolConverter",
    "UserQueryProvider",
    "WorkspaceSystemPromptProvider",
]
