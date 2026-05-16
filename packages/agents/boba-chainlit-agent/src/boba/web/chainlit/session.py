"""Сборка агента и запуск одного запроса с UI-sink'ом."""

from __future__ import annotations

from pathlib import Path

from boba.agent import AgentBuilder, TurnBuilder
from boba.agent.agent import Agent
from boba.agent.messages import MessageService
from boba.agent.prompt_providers import PromptLoader

# from boba.agent.turn.reducers import (
#     RememberUserQueryReducer,
# )
from boba.agent.workspace_fs import FsPromptWorkspaceRegistry
from boba.llm.builder import LLMBuilder
from boba.provider.openai import (
    CurlTraceChatCompletionObserver,
    use_openai,
)
from boba.web.chainlit.bridge import ChainlitBridgeSink
from boba.web.chainlit.config import ChainlitConfig
from boba.web.chainlit.infra import (
    AppConfig,
    log_context,
)
from boba.workspace.contract import (
    HistoryWorkspaceRegistry,
    ProjectWorkspaceRegistry,
    ProjectWorkspaceShell,
    PromptWorkspaceId,
    WorkspaceId,
)


class ChatSession:
    """Per-workspace обёртка: один Agent, привязанный к конкретному workspace_id."""

    def __init__(
        self,
        workspace_id: WorkspaceId,
        builder: AgentBuilder,
        project_workspaces: ProjectWorkspaceRegistry,
        history_workspaces: HistoryWorkspaceRegistry,
        message_service: MessageService,
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

        prompt_workspace = FsPromptWorkspaceRegistry(
            root=Path(app.prompts.dir),
        ).get_or_create(PromptWorkspaceId("prompts"))
        prompt_loader = PromptLoader(prompt_workspace)

        turn = (
            TurnBuilder()
            .with_model(self._chainlit_config.model)
            .with_prompts(prompt_loader.prompt_providers())
            .use_default_reducers()
            # .use_reducer(RememberUserQueryReducer())
        )
        self._agent: Agent = (
            builder.with_extension(ProjectWorkspaceShell, project_shell)
            .with_llm(llm)
            .with_messages(message_service)
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
