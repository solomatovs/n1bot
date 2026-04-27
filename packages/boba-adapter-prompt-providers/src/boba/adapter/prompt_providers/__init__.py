"""Реализации PromptProvider и file-based loader."""

from boba.adapter.prompt_providers.config import PromptsSection
from boba.adapter.prompt_providers.loader import PromptLoader, PromptLoadError
from boba.adapter.prompt_providers.providers import (
    EnvironmentPromptProvider,
    FilePromptProvider,
    GitPromptProvider,
    StaticPromptProvider,
    WorkspaceSystemPromptProvider,
)

__all__ = [
    "EnvironmentPromptProvider",
    "FilePromptProvider",
    "GitPromptProvider",
    "PromptLoadError",
    "PromptLoader",
    "PromptsSection",
    "StaticPromptProvider",
    "WorkspaceSystemPromptProvider",
]
