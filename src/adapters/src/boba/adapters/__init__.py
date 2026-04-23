"""Публичный API adapters-слоя.

Реэкспортирует фасадные адаптеры: sink'и, workspace/messages реализации,
OpenAI-совместимый клиент LLM, провайдеры промптов и инструментов.
Внутренние классы конкретных файлов (``IsServerError``, ``RoleSource``,
``ThinkingSource`` и т.п. из :mod:`.openai_completion`) здесь не
выставляются — это детали реализации, импортируй их напрямую из
сабмодуля при необходимости.

Импорты короткой формой::

    from boba.adapters import OpenAIMiddleware, StupidRetryLLMMiddleware
    from boba.adapters import JsonLinesMessageService, ConsoleSink
    from boba.adapters import FsWorkspaceRegistry, FsWorkspaceShell
"""

# from boba.adapters.aggregating_llm_request_factory import AggregatingLLMRequestFactory
from boba.adapters.console_sink import ConsoleSink
from boba.adapters.fs_workspace import (
    FsHistoryWorkspaceRegistry,
    FsHistoryWorkspaceShell,
    FsProjectWorkspaceRegistry,
    FsProjectWorkspaceShell,
    FsScratchWorkspaceRegistry,
    FsScratchWorkspaceShell,
    FsWorkspaceRegistry,
    FsWorkspaceShell,
)
from boba.adapters.in_memory_messages import InMemoryMessageService
from boba.adapters.jsonl_messages import JsonLinesMessageService
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
    # "AggregatingLLMRequestFactory",
    "CompositeRawLLMObserver",
    "ConsoleSink",
    "EnvironmentPromptProvider",
    "FileContentObserver",
    "FilePromptProvider",
    "FileRawLLMObserver",
    "FromOpenAIChunkConverter",
    "FsHistoryWorkspaceRegistry",
    "FsHistoryWorkspaceShell",
    "FsProjectWorkspaceRegistry",
    "FsProjectWorkspaceShell",
    "FsScratchWorkspaceRegistry",
    "FsScratchWorkspaceShell",
    "FsWorkspaceRegistry",
    "FsWorkspaceShell",
    "GitPromptProvider",
    "IDESelectionProvider",
    "InMemoryMessageService",
    "JsonLinesMessageService",
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
