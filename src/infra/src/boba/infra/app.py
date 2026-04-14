"""Dishka-провайдеры приложения (Scope.APP)."""

from __future__ import annotations

from pathlib import Path

from dishka import Provider, Scope, provide

from boba.adapters.fs_workspace import FsWorkspaceRegistry
from boba.domain.config import AppConfig
from boba.domain.core.workspace import WorkspaceRegistry


class AppProvider(Provider):
    """Singleton-сервисы: конфигурация."""

    scope = Scope.APP

    def __init__(self, config: AppConfig) -> None:
        super().__init__()
        self._config = config

    @provide
    def config(self) -> AppConfig:
        return self._config


class WorkspaceProvider(Provider):
    """Singleton: реестр workspace'ов."""

    scope = Scope.APP

    @provide
    def registry(self, config: AppConfig) -> WorkspaceRegistry:
        base_dir = Path(config.workspace_base_dir)
        return FsWorkspaceRegistry(base_dir)
