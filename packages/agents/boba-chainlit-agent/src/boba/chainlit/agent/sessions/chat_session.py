"""Сборка агента и запуск одного запроса с UI-sink'ом."""

from __future__ import annotations

from pathlib import Path

from boba.agent import AgentBuilder, TurnBuilder
from boba.agent.agent import Agent
from boba.agent.history import JsonLinesHistoryService
from boba.agent.workspace_fs import FsPromptWorkspaceRegistry
from boba.chainlit.agent.config import ChainlitConfig
from boba.chainlit.agent.logging import log_context
from boba.chainlit.agent.rendering.bridge import ChainlitBridgeSink
from boba.llm.builder import LLMBuilder
from boba.llm.middleware import JsonContentToolCallMiddleware
from boba.provider.openai import (
    CurlTraceChatCompletionObserver,
    HttpTraceChatCompletionObserver,
    use_openai,
)
from boba.workspace.contract import (
    HistoryWorkspaceRegistry,
    HistoryWorkspaceShell,
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
    ) -> None:
        self._workspace_id = workspace_id
        self._chainlit_config = ChainlitConfig.load()
        rt = self._chainlit_config.runtime

        project_shell = project_workspaces.get_or_create(workspace_id)
        if not isinstance(project_shell, ProjectWorkspaceShell):
            msg = (
                f"FsProjectWorkspaceRegistry returned {type(project_shell).__name__},"
                f" expected ProjectWorkspaceShell"
            )
            raise TypeError(msg)
        self._project_shell = project_shell

        history_shell = history_workspaces.get_or_create(workspace_id)
        if not isinstance(history_shell, HistoryWorkspaceShell):
            msg = (
                f"FsHistoryWorkspaceRegistry returned {type(history_shell).__name__},"
                f" expected HistoryWorkspaceShell"
            )
            raise TypeError(msg)
        llm = (
            LLMBuilder()
            .add_observer(CurlTraceChatCompletionObserver(history_shell))
            .add_observer(HttpTraceChatCompletionObserver(history_shell))
            .use_provider_middleware(JsonContentToolCallMiddleware)
            .build(use_openai(rt.openai))
        )

        system_prompt_workspace = FsPromptWorkspaceRegistry(
            root=Path(rt.system_prompt_dir),
        ).get_or_create(PromptWorkspaceId("prompts"))

        turn = TurnBuilder(rt.model).system_prompt_from_directory(
            system_prompt_workspace
        )

        def _provide_project_workspace() -> ProjectWorkspaceShell:
            """DI factory для pre-built `ProjectWorkspaceShell`.

            Замыкание над per-session shell'ом: каждый ChatSession имеет
            свой `project_shell`, привязанный к своему workspace_id, и
            свой `AgentBuilder` → свой DI-контейнер.
            """
            return project_shell

        def _provide_history_workspace() -> HistoryWorkspaceShell:
            """DI factory для per-session history workspace.

            Используется `JsonLinesHistoryService` (через `use_history` ниже),
            который Dishka конструирует рекурсивно — резолвит `workspace`
            через эту factory.
            """
            return history_shell

        self._agent: Agent = (
            builder.register_provider(_provide_project_workspace)
            .register_provider(_provide_history_workspace)
            .use_history(JsonLinesHistoryService)
            .use_llm(llm)
            .use_compact_history(max_messages=rt.max_messages)
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
