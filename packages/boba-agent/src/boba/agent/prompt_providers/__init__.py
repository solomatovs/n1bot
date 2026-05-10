"""I/O-реализации PromptProvider (file/git/env/workspace) и file-based loader."""

from boba.agent.prompt_providers.config import PromptsConfig
from boba.agent.prompt_providers.loader import PromptLoader, PromptLoadError
from boba.agent.prompt_providers.providers import (
    EnvironmentPromptProvider,
    FilePromptProvider,
    GitPromptProvider,
    WorkspaceSystemPromptProvider,
)

__all__ = [
    "EnvironmentPromptProvider",
    "FilePromptProvider",
    "GitPromptProvider",
    "PromptLoadError",
    "PromptLoader",
    "PromptsConfig",
    "WorkspaceSystemPromptProvider",
]
