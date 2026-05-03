"""Сборка агента и запуск одного запроса с UI-sink'ом."""

from __future__ import annotations

from pathlib import Path

from boba.adapter.fs_workspace import (
    FsHistoryWorkspaceRegistry,
    FsProjectWorkspaceRegistry,
    FsPromptWorkspaceRegistry,
    WorkspacesSection,
)
from boba.adapter.messages import InMemoryMessageService
from boba.adapter.openai import (
    LLMTransportSection,
    TranscriptChatCompletionObserver,
    create_llm_source,
)
from boba.adapter.prompt_providers import PromptLoader, PromptsSection
from boba.agent.events import AgentEvent
from boba.agent.models import AgentContext, AgentRequest
from boba.config.app import AppConfig
from boba.llm.models import RequestId
from boba.patterns import StreamSink
from boba.tools import ExtensionContext, ToolContext, ToolPluginLoader
from boba.web.chainlit.config import ChainlitConfig, ChainlitSection
from boba.web.chainlit.infra import (
    AgentComponents,
    AgentSection,
    AppCoreSection,
    configure_logging,
    create_agent,
    log_context,
)
from boba.workspace import (
    ProjectWorkspaceShell,
    PromptWorkspaceId,
    WorkspaceId,
)


class ChatSession:
    """One-shot обёртка: конфиг + workspace registry; агент пересобирается на каждый run."""  # noqa: E501

    _app: AppConfig | None = None

    @classmethod
    def set_app(cls, app: AppConfig) -> None:
        """Инжектит application-level AppConfig до первого ChatSession()."""
        cls._app = app

    def __init__(self) -> None:
        if ChatSession._app is None:
            msg = (
                "ChatSession instantiated before ChatSession.set_app() — "
                "bootstrap must call set_app() in __main__.main()."
            )
            raise RuntimeError(msg)
        app = ChatSession._app

        core = app.section(AppCoreSection)
        configure_logging(core.log_level, core.log_file)

        self._app = app
        self._workspaces_cfg = app.section(WorkspacesSection)
        self._llm_cfg = app.section(LLMTransportSection)
        self._prompts_dir = app.section(PromptsSection)
        self._agent_config = app.section(AgentSection)
        self._chainlit_config: ChainlitConfig = app.section(ChainlitSection)

        self._workspaces = FsProjectWorkspaceRegistry(
            base_dir=Path(self._workspaces_cfg.base_dir),
            subdir=self._workspaces_cfg.user_subdir,
        )

        self._history_workspaces = FsHistoryWorkspaceRegistry(
            base_dir=Path(self._workspaces_cfg.base_dir),
            subdir=self._workspaces_cfg.system_subdir,
        )

        prompt_workspace = FsPromptWorkspaceRegistry(
            root=Path(self._prompts_dir),
        ).get_or_create(PromptWorkspaceId("prompts"))
        prompt_loader = PromptLoader(prompt_workspace)
        self._prompt_providers = prompt_loader.prompt_providers()

        tool_loader = ToolPluginLoader(ExtensionContext(config=app))
        self._tools_service = tool_loader.tools_service()

    @property
    def models(self) -> list[str]:
        """Список LLM-моделей для UI ChatSettings."""
        return self._chainlit_config.models

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
        extra_sink: StreamSink[AgentContext, AgentEvent],
        *,
        model: str,
    ) -> None:
        """Запустить агентский цикл; model выбирается только на стороне UI."""
        # workspace подтягивается заранее, чтобы последующий upload в тот же id работал.
        project_workspace = self._workspaces.get_or_create(workspace_id)
        request_id = RequestId.new()

        # ToolContext — per-request DI: прокидывает workspace в Tool.execute.
        tool_ctx = ToolContext(project_workspace=project_workspace)

        history_workspace = self._history_workspaces.get_or_create(workspace_id)
        observer = TranscriptChatCompletionObserver(history_workspace)
        llm_source = create_llm_source(self._llm_cfg, observer)
        agent = create_agent(
            llm_source=llm_source,
            components=AgentComponents(
                agent_config=self._agent_config,
                prompt_providers=self._prompt_providers,
                message_service=InMemoryMessageService(),
                tools_service=self._tools_service,
            ),
            tool_ctx=tool_ctx,
            sink=extra_sink,
        )

        request = AgentRequest(
            model=model,
            request_id=request_id,
        )
        with log_context(
            request_id=request_id.to_wire(),
            workspace_id=workspace_id.to_wire(),
        ):
            agent.run(self._agent_config, request, query)
