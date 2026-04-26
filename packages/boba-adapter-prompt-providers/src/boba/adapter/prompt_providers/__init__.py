"""Конкретные реализации PromptProvider + file-based loader.

Состав пакета:

- _providers — провайдеры (Static/File/Environment/Git/
  WorkspaceSystem), пишут в system-prompt; USER-блок caller передаёт
  через query.
- _loader — PromptLoader, discovery *.md/*.txt
  из PromptWorkspaceShell в коллекцию
  StaticPromptProvider.
- _config — PromptsSection (путь до prompts-dir).

Пакет — отдельный pip-package; основной boba от него НЕ зависит.
Короткие импорты::

    from boba.adapter.prompt_providers import (
        PromptLoader, PromptsSection,
        StaticPromptProvider, FilePromptProvider,
        EnvironmentPromptProvider, GitPromptProvider,
        WorkspaceSystemPromptProvider,
    )
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
