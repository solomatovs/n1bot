"""Сборка агента и запуск одного запроса с UI-sink'ом."""

from __future__ import annotations

from pathlib import Path

from boba.agent import AgentBuilder, AgentInput, InMemoryMessageService
from boba.agent.orchestrator import Agent, AgentRequest
from boba.agent.prompt_providers import PromptLoader
from boba.agent.workspace_fs import FsPromptWorkspaceRegistry
from boba.llm.models import RequestId
from boba.provider.openai import (
    CurlTraceChatCompletionObserver,
    OpenAIChatVisitor,
    create_llm_source,
)
from boba.web.chainlit.bridge import ChainlitBridgeSink
from boba.web.chainlit.config import ChainlitConfig
from boba.web.chainlit.infra import (
    AppConfig,
    configure_logging,
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
    ) -> None:
        bundle = builder.config_bundle()
        if bundle is None:
            msg = (
                "ChatSession: AgentBuilder must be initialized "
                "via with_config_bundle(...)"
            )
            raise ValueError(msg)
        app = bundle.get(AppConfig, "agent")
        configure_logging(app.core.log_level, app.core.log_file)

        self._workspace_id = workspace_id
        self._agent_config = app.runtime
        self._chainlit_config = bundle.get(ChainlitConfig, "chainlit")

        project_shell = project_workspaces.get_or_create(workspace_id)
        if not isinstance(project_shell, ProjectWorkspaceShell):
            msg = (
                f"FsProjectWorkspaceRegistry returned {type(project_shell).__name__},"
                f" expected ProjectWorkspaceShell"
            )
            raise TypeError(msg)
        self._project_shell = project_shell

        history_shell = history_workspaces.get_or_create(workspace_id)
        observer = CurlTraceChatCompletionObserver(history_shell)
        llm_source = create_llm_source(app.openai, observer)

        prompt_workspace = FsPromptWorkspaceRegistry(
            root=Path(app.prompts.dir),
        ).get_or_create(PromptWorkspaceId("prompts"))
        prompt_loader = PromptLoader(prompt_workspace)

        self._agent: Agent = (
            builder
            .with_extension(ProjectWorkspaceShell, project_shell)
            .with_llm(llm_source)
            .with_tool_result_visitor(OpenAIChatVisitor())
            .with_messages(InMemoryMessageService())
            .with_prompts(prompt_loader.prompt_providers())
            .with_config(self._agent_config)
            .build()
        )

    def project_workspace(self) -> ProjectWorkspaceShell:
        """Project-workspace сессии — тот же, куда смотрят file-tools агента."""
        return self._project_shell

    def run(self, query: str, extra_sink: ChainlitBridgeSink) -> None:
        """Запустить агентский цикл; модель берётся из chainlit-конфига."""
        request_id = RequestId.new()
        agent_input = AgentInput(
            request=AgentRequest(
                model=self._chainlit_config.model,
                request_id=request_id,
                query=query,
            ),
            config=self._agent_config,
        )
        with log_context(
            request_id=request_id.to_wire(),
            workspace_id=self._workspace_id.to_wire(),
        ):
            for event in self._agent.stream(agent_input):
                extra_sink.handle(event)
