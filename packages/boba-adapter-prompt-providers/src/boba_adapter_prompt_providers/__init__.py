"""Конкретные реализации :class:`PromptProvider` + file-based loader.

Состав пакета:

- :mod:`._providers` — провайдеры (Static/File/Environment/Git/
  WorkspaceSystem), пишут в system-prompt; USER-блок caller передаёт
  через :attr:`AgentRequest.query`.
- :mod:`._loader` — :class:`PromptLoader`, discovery ``*.md``/``*.txt``
  из :class:`PromptWorkspaceShell` в коллекцию
  :class:`StaticPromptProvider`.
- :mod:`._config` — :class:`PromptsSection` (путь до prompts-dir).

Пакет — отдельный pip-package; основной ``boba`` от него НЕ зависит.
Короткие импорты::

    from boba_adapter_prompt_providers import (
        PromptLoader, PromptsSection,
        StaticPromptProvider, FilePromptProvider,
        EnvironmentPromptProvider, GitPromptProvider,
        WorkspaceSystemPromptProvider,
    )
"""

from boba_adapter_prompt_providers.config import PromptsSection
from boba_adapter_prompt_providers.loader import PromptLoader, PromptLoadError
from boba_adapter_prompt_providers.providers import (
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
