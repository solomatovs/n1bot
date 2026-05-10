"""Сборка агента и запуск одного запроса с UI-sink'ом."""

from __future__ import annotations

from pathlib import Path

from boba.agent import AgentBuilder, AgentInput, InMemoryMessageService
from boba.agent.orchestrator import AgentConfig, AgentRequest
from boba.config.bundle import ConfigBundle
from boba.llm.models import RequestId
from boba.plugin import ExtensionContext as PluginCtx
from boba.plugin import install_plugins
from boba.plugin.discovery import discover_plugins
from boba.agent.prompt_providers import PromptLoader, PromptsConfig
from boba.provider.openai import (
    CurlTraceChatCompletionObserver,
    OpenAIChatVisitor,
    OpenAIConfig,
    create_llm_source,
)
from boba.tools.domain import ToolContext
from boba.tools.framework import ToolsService
from boba.web.chainlit.bridge import ChainlitBridgeSink
from boba.web.chainlit.config import ChainlitConfig
from boba.web.chainlit.infra import (
    AppCoreConfig,
    configure_logging,
    log_context,
)
from boba.workspace.contract import (
    ProjectWorkspaceShell,
    PromptWorkspaceId,
    WorkspaceId,
)
from boba.agent.workspace_fs import (
    FsHistoryWorkspaceRegistry,
    FsProjectWorkspaceRegistry,
    FsPromptWorkspaceRegistry,
    WorkspaceLayout,
)


class ChatSession:
    """One-shot обёртка: конфиг + workspace registry; агент пересобирается на каждый run."""  # noqa: E501

    _bundle: ConfigBundle | None = None

    @classmethod
    def set_bundle(cls, bundle: ConfigBundle) -> None:
        """Инжектит application-level ConfigBundle до первого ChatSession()."""
        cls._bundle = bundle

    def __init__(self) -> None:
        if ChatSession._bundle is None:
            msg = (
                "ChatSession instantiated before ChatSession.set_bundle() — "
                "bootstrap must call set_bundle() in __main__.main()."
            )
            raise RuntimeError(msg)
        bundle = ChatSession._bundle

        core = bundle.get(AppCoreConfig, "app")
        configure_logging(core.log_level, core.log_file)

        self._workspaces_cfg = bundle.get(WorkspaceLayout, "workspaces")
        self._llm_cfg = bundle.get(OpenAIConfig, "provider.openai")
        self._prompts = bundle.get(PromptsConfig, "prompts")
        self._agent_config = bundle.get(AgentConfig, "agent")
        self._chainlit_config = bundle.get(ChainlitConfig, "chainlit")

        self._workspaces = FsProjectWorkspaceRegistry(
            base_dir=Path(self._workspaces_cfg.base_dir),
            subdir=self._workspaces_cfg.user_subdir,
        )

        self._history_workspaces = FsHistoryWorkspaceRegistry(
            base_dir=Path(self._workspaces_cfg.base_dir),
            subdir=self._workspaces_cfg.system_subdir,
        )

        prompt_workspace = FsPromptWorkspaceRegistry(
            root=Path(self._prompts.dir),
        ).get_or_create(PromptWorkspaceId("prompts"))
        prompt_loader = PromptLoader(prompt_workspace)
        self._prompt_providers = prompt_loader.prompt_providers()

        self._tools_service = ToolsService.from_sources(
            install_plugins(bundle, list(discover_plugins()), PluginCtx()),
        )
        self._tool_result_visitor = OpenAIChatVisitor()

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
        extra_sink: ChainlitBridgeSink,
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
        observer = CurlTraceChatCompletionObserver(history_workspace)
        llm_source = create_llm_source(self._llm_cfg, observer)
        agent = (
            AgentBuilder()
            .with_llm(llm_source)
            .with_tools(self._tools_service)
            .with_tool_result_visitor(self._tool_result_visitor)
            .with_messages(InMemoryMessageService())
            .with_prompts(self._prompt_providers)
            .with_config(self._agent_config)
            .build(tool_ctx=tool_ctx)
        )

        request = AgentRequest(
            model=model,
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
