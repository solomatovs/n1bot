"""Конкретные реализации PromptProvider + file-based loader.

- providers — Static/File/Environment/Git/WorkspaceSystem (system-prompt);
- loader — PromptLoader, discovery *.md/*.txt из PromptWorkspaceShell;
- config — PromptsSection (путь до prompts-dir).

Отдельный pip-пакет; core boba от него не зависит.
"""

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
