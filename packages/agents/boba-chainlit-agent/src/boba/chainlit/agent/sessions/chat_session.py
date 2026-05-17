"""Сборка агента и запуск одного запроса с UI-sink'ом."""

from __future__ import annotations

from pathlib import Path

from boba.agent import AgentBuilder, TurnBuilder
from boba.agent.agent import Agent
from boba.agent.history import HistoryService
from boba.agent.workspace_fs import FsPromptWorkspaceRegistry
from boba.chainlit.agent.config import AppConfig, ChainlitConfig
from boba.chainlit.agent.logging import log_context
from boba.chainlit.agent.rendering.bridge import ChainlitBridgeSink
from boba.llm.builder import LLMBuilder
from boba.provider.openai import (
    CurlTraceChatCompletionObserver,
    use_openai,
)
from boba.workspace.contract import (
    HistoryWorkspaceRegistry,
    ProjectWorkspaceRegistry,
    ProjectWorkspaceShell,
    PromptWorkspaceId,
    WorkspaceId,
)

__all__ = ["ChatSession"]


class ChatSession:
    """Per-workspace обёртка: один Agent, привязанный к конкретному workspace_id."""

    def __init__(
        self,
        workspace_id: WorkspaceId,
        builder: AgentBuilder,
        project_workspaces: ProjectWorkspaceRegistry,
        history_workspaces: HistoryWorkspaceRegistry,
        history_service: HistoryService,
    ) -> None:
        app = AppConfig.load()

        self._workspace_id = workspace_id
        self._chainlit_config = ChainlitConfig.load()

        project_shell = project_workspaces.get_or_create(workspace_id)
        if not isinstance(project_shell, ProjectWorkspaceShell):
            msg = (
                f"FsProjectWorkspaceRegistry returned {type(project_shell).__name__},"
                f" expected ProjectWorkspaceShell"
            )
            raise TypeError(msg)
        self._project_shell = project_shell

        history_shell = history_workspaces.get_or_create(workspace_id)
        llm = (
            LLMBuilder()
            .add_observer(CurlTraceChatCompletionObserver(history_shell))
            .pipe(use_openai, app.openai)
            .build()
        )

        system_prompt_workspace = FsPromptWorkspaceRegistry(
            root=Path(app.system_prompt_dir),
        ).get_or_create(PromptWorkspaceId("prompts"))

        turn = TurnBuilder(self._chainlit_config.model).system_prompt_from_directory(
            system_prompt_workspace
        )
        self._agent: Agent = (
            builder.with_extension(ProjectWorkspaceShell, project_shell)
            .with_llm(llm)
            .with_history(history_service)
            .use_turn(turn)
            .build()
        )

    def project_workspace(self) -> ProjectWorkspaceShell:
        """Project-workspace сессии — тот же, куда смотрят file-tools агента."""
        return self._project_shell

    def run(self, query: str, extra_sink: ChainlitBridgeSink) -> None:
        """Запустить агентский цикл; модель задана при сборке."""
        with log_context(workspace_id=self._workspace_id):
            for event in self._agent.stream(query):
                extra_sink.handle(event)
