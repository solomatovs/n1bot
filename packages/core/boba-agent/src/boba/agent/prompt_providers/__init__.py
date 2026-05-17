"""I/O-реализации PromptProvider (file/git/env, workspace-based)."""

from boba.agent.prompt_providers.config import PromptsConfig
from boba.agent.prompt_providers.providers import (
    DirectoryPromptProvider,
    EnvironmentPromptProvider,
    FilePromptProvider,
    GitPromptProvider,
    StaticPromptProvider,
)

__all__ = [
    "DirectoryPromptProvider",
    "EnvironmentPromptProvider",
    "FilePromptProvider",
    "GitPromptProvider",
    "PromptsConfig",
    "StaticPromptProvider",
]
