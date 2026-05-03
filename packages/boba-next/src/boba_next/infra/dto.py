"""Общие DTO инфра-уровня: LLMConfig, WorkspaceLayout."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

__all__ = ["LLMConfig", "WorkspaceLayout"]


@dataclass(frozen=True)
class LLMConfig:
    """Конфигурация LLM-клиента (транспорт)."""

    base_url: str = "http://localhost:11434/v1"
    api_key: str = "ollama"


@dataclass(frozen=True)
class WorkspaceLayout:
    """Раскладка namespace'ов workspace'а относительно base_dir."""

    base_dir: str = "./workspaces"
    user_subdir: str = "user"
    system_subdir: str = "system"
    tmp_subdir: str = "tmp"

    def root(self) -> Path:
        return Path(self.base_dir)
