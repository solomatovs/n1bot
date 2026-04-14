"""Dishka-провайдеры приложения."""

from __future__ import annotations

from pathlib import Path
from uuid import UUID

from dishka import Provider, Scope, from_context, provide

from boba.adapters.fs_workspace import FsWorkspaceManager
from boba.adapters.in_memory_messages import InMemoryMessageService
from boba.adapters.openai_completion import OpenAICompletionService
from boba.domain.agent.loop import AgentLoop
from boba.domain.agent.models import AgentConfig
from boba.domain.config import AppConfig
from boba.domain.core.messages import MessageService
from boba.domain.core.workspace import WorkspaceManager, WorkspaceService
from boba.domain.llm.llm import LLMCompletionService


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

    @provide
    def llm_completion_service(self, config: AppConfig) -> LLMCompletionService:
        return OpenAICompletionService(config.llm)

    @provide
    def agent_config(self, config: AppConfig) -> AgentConfig:
        return config.agent


class RequestProvider(Provider):
    """Per-request: workspace service, message service."""

    scope = Scope.REQUEST

    workspace_id = from_context(provides=UUID | None, scope=Scope.REQUEST)

    @provide
    def workspace_service(
        self,
        workspace_id: UUID | None,
        manager: WorkspaceManager,
    ) -> WorkspaceService:
        return manager.get_or_create(workspace_id)

    @provide
    def message_service(self) -> MessageService:
        return InMemoryMessageService()

    @provide
    def agent_loop(
        self,
        agent_config: AgentConfig,
        message_service: MessageService,
        llm: LLMCompletionService,
    ) -> AgentLoop:
        return AgentLoop(
            config=agent_config,
            message_service=message_service,
            llm=llm,
        )
