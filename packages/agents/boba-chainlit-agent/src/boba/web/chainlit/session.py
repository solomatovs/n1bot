"""Сборка агента и запуск одного запроса с UI-sink'ом."""

from __future__ import annotations

from pathlib import Path

from boba.agent import AgentBuilder, AgentInput, InMemoryMessageService
from boba.agent.orchestrator import AgentRequest
from boba.agent.prompt_providers import PromptLoader
from boba.agent.workspace_fs import (
    FsPromptWorkspaceRegistry,
)
from boba.llm.models import RequestId
from boba.provider.openai import (
    CurlTraceChatCompletionObserver,
    OpenAIChatVisitor,
    create_llm_source,
)
from boba.tools.domain import ToolContext
from boba.tools.framework import ToolExecutor
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
    """One-shot обёртка: конфиг + workspace registry; агент пересобирается на каждый run."""  # noqa: E501

    _builder: AgentBuilder | None = None
    _project_workspaces: ProjectWorkspaceRegistry | None = None
    _history_workspaces: HistoryWorkspaceRegistry | None = None

    @classmethod
    def set_builder(
        cls,
        builder: AgentBuilder,
        *,
        project_workspaces: ProjectWorkspaceRegistry,
        history_workspaces: HistoryWorkspaceRegistry,
    ) -> None:
        """
        Инжектит application-level AgentBuilder + registry'и до первого ChatSession()
        """
        cls._builder = builder
        cls._project_workspaces = project_workspaces
        cls._history_workspaces = history_workspaces

    def __init__(self) -> None:
        if (
            ChatSession._builder is None
            or ChatSession._project_workspaces is None
            or ChatSession._history_workspaces is None
        ):
            msg = (
                "ChatSession instantiated before ChatSession.set_builder() — "
                "bootstrap must call set_builder() in __main__.main()."
            )
            raise RuntimeError(msg)
        builder = ChatSession._builder
        bundle = builder.bundle()

        app = bundle.get(AppConfig, "agent")
        configure_logging(app.core.log_level, app.core.log_file)

        self._llm_cfg = app.openai
        self._agent_config = app.runtime
        self._chainlit_config = bundle.get(ChainlitConfig, "chainlit")

        self._workspaces = ChatSession._project_workspaces
        self._history_workspaces = ChatSession._history_workspaces

        prompt_workspace = FsPromptWorkspaceRegistry(
            root=Path(app.prompts.dir),
        ).get_or_create(PromptWorkspaceId("prompts"))
        prompt_loader = PromptLoader(prompt_workspace)
        self._prompt_providers = prompt_loader.prompt_providers()

        self._tool_executor: ToolExecutor = builder.tool_executor()
        self._tool_result_visitor = OpenAIChatVisitor()

    def project_workspace(self, workspace_id: WorkspaceId) -> ProjectWorkspaceShell:
        """Project-workspace пользователя — тот же, куда смотрят file-tools агента."""
        shell = self._workspaces.get_or_create(workspace_id)
        if not isinstance(shell, ProjectWorkspaceShell):
            msg = (
                f"FsProjectWorkspaceRegistry returned "
                f"{type(shell).__name__}, expected ProjectWorkspaceShell"
            )
            raise TypeError(msg)
        return shell

    def run(
        self,
        workspace_id: WorkspaceId,
        query: str,
        extra_sink: ChainlitBridgeSink,
    ) -> None:
        """Запустить агентский цикл; модель берётся из chainlit-конфига."""
        # workspace подтягивается заранее, чтобы последующий upload в тот же id работал.
        self._workspaces.get_or_create(workspace_id)
        request_id = RequestId.new()

        tool_ctx = ToolContext(workspace_id=workspace_id)

        history_workspace = self._history_workspaces.get_or_create(workspace_id)
        observer = CurlTraceChatCompletionObserver(history_workspace)
        llm_source = create_llm_source(self._llm_cfg, observer)
        agent = (
            AgentBuilder()
            .with_llm(llm_source)
            .with_tools(self._tool_executor)
            .with_tool_result_visitor(self._tool_result_visitor)
            .with_messages(InMemoryMessageService())
            .with_prompts(self._prompt_providers)
            .with_config(self._agent_config)
            .build(tool_ctx=tool_ctx)
        )

        request = AgentRequest(
            model=self._chainlit_config.model,
            request_id=request_id,
            query=query,
        )
        agent_input = AgentInput(
            request=request,
            config=self._agent_config,
        )
        with log_context(
            request_id=request_id.to_wire(),
            workspace_id=workspace_id.to_wire(),
        ):
            for event in agent.stream(agent_input):
                extra_sink.handle(event)
