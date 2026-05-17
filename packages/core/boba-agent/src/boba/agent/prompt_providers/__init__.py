"""Реализации PromptProvider (file/dir/env/git, computed, wrapping)."""

from boba.agent.prompt_providers.providers import (
    CallablePromptProvider,
    DirectoryPromptProvider,
    EnvironmentPromptProvider,
    FilePromptProvider,
    GitPromptProvider,
    StaticPromptProvider,
    WrappingPromptProvider,
)

__all__ = [
    "CallablePromptProvider",
    "DirectoryPromptProvider",
    "EnvironmentPromptProvider",
    "FilePromptProvider",
    "GitPromptProvider",
    "StaticPromptProvider",
    "WrappingPromptProvider",
]
