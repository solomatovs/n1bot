"""Адаптеры внешних зависимостей (FS, OpenAI, JSONL-хранилище и пр.).

Подпакеты:

- :mod:`boba.adapters.llm` — OpenAI-терминал и конвертеры.

Tool-ы агента живут вне пакета — в директории plugins/, грузятся
:class:`boba.infra.plugins.PluginLoader` через ``BOBA_PLUGINS_DIR``.

На верхнем уровне реэкспортированы адаптеры из плоских модулей
(``console_sink``, ``fs_workspace``, ``prompt_providers``,
``jsonl_messages``, ``raw_llm_observer`` и т.п.).

Короткие импорты::

    from boba.adapters import TextOutSink, FsProjectWorkspaceRegistry
    from boba.adapters import JsonLinesMessageService, StaticPromptProvider
    from boba.adapters.llm import OpenAITerminal

Имя ``MultiKeyReasoningExtractor`` есть и в
:mod:`boba.adapters.raw_llm_observer`, и в
:mod:`boba.adapters.llm.openai_response` — это разные реализации, на
плоский уровень ни одну не выносим, импортируйте из подмодуля.
"""

from boba.adapters.console_sink import TextOutSink
from boba.adapters.fs_workspace import (
    FsHistoryWorkspaceRegistry,
    FsHistoryWorkspaceShell,
    FsProjectWorkspaceRegistry,
    FsProjectWorkspaceShell,
    FsScratchWorkspaceRegistry,
    FsScratchWorkspaceShell,
    FsWorkspaceRegistry,
    FsWorkspaceShell,
    WorkspacePath,
)
from boba.adapters.growbuffer import GrowBuffer
from boba.adapters.in_memory_messages import InMemoryMessageService
from boba.adapters.jsonl_messages import JsonLinesMessageService
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
    RawLLMObserver,
    RequestOutcome,
)
from boba.adapters.tool_call_reindexer import DuplicateToolCallIndexReindexer
from boba.adapters.tool_providers import StaticToolSource

__all__ = [
    "CompositeRawLLMObserver",
    "DuplicateToolCallIndexReindexer",
    "EnvironmentPromptProvider",
    "FileContentObserver",
    "FilePromptProvider",
    "FileRawLLMObserver",
    "FsHistoryWorkspaceRegistry",
    "FsHistoryWorkspaceShell",
    "FsProjectWorkspaceRegistry",
    "FsProjectWorkspaceShell",
    "FsScratchWorkspaceRegistry",
    "FsScratchWorkspaceShell",
    "FsWorkspaceRegistry",
    "FsWorkspaceShell",
    "GitPromptProvider",
    "GrowBuffer",
    "IDESelectionProvider",
    "InMemoryMessageService",
    "JsonLinesMessageService",
    "MetricsRawLLMObserver",
    "RawLLMObserver",
    "RequestOutcome",
    "StaticPromptProvider",
    "StaticToolSource",
    "TemplateProvider",
    "TextOutSink",
    "UserQueryProvider",
    "WorkspacePath",
    "WorkspaceSystemPromptProvider",
]
