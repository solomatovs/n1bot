"""Dishka-провайдеры приложения."""

from __future__ import annotations

from pathlib import Path
from uuid import UUID

from dishka import Provider, Scope, from_context, provide

from boba.adapters.fs_workspace import FsWorkspaceManager
from boba.domain.config import AppConfig
from boba.domain.core.workspace import WorkspaceManager, WorkspaceService


class AppProvider(Provider):
    """Singleton-сервисы: конфигурация."""

    scope = Scope.APP

    def __init__(self, config: AppConfig) -> None:
        super().__init__()
        self._config = config

    @provide
    def config(self) -> AppConfig:
        return self._config

    @provide
    def workspace_manager(self, config: AppConfig) -> WorkspaceManager:
        return FsWorkspaceManager(Path(config.workspace_base_dir))


class RequestProvider(Provider):
    """Per-request: workspace service."""

    scope = Scope.REQUEST

    workspace_id = from_context(provides=UUID | None, scope=Scope.REQUEST)

    @provide
    def workspace_service(
        self,
        workspace_id: UUID | None,
        manager: WorkspaceManager,
    ) -> WorkspaceService:
        return manager.get_or_create(workspace_id)
