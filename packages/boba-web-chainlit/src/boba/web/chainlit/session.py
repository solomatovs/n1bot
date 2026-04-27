"""Сборка агента и запуск одного запроса с UI-sink'ом.

В отличие от старой версии (на Dishka-контейнере и request_scope)
эта реализация не тянет DI-инфраструктуру — boba её не использует.
Собирается вручную: один раз в конструкторе — workspace registry и
конфиг; на каждый run — свежий source через
create_agent_source, Agent(source, UI-sink), синхронный
прогон.

Один инстанс на процесс Chainlit: конфиг приходит из bundle, который
собирает __main__ ДО запуска chainlit-сервера
и инжектит через set_bundle. Состояние сессии
(WorkspaceId) живёт в cl.user_session.
"""

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
from boba.domain.agent.events import AgentEvent
from boba.domain.agent.models import AgentContext, AgentRequest
from boba.domain.config import AppConfig
from boba.domain.core.patterns import StreamSink
from boba.domain.core.tools import ToolContext
from boba.domain.core.workspace import (
    ProjectWorkspaceShell,
    PromptWorkspaceId,
    WorkspaceId,
)
from boba.domain.llm.models import RequestId
from boba.infra import (
    AgentComponents,
    AgentSection,
    AppCoreSection,
    ConfigBundle,
    ExtensionContext,
    ToolPluginLoader,
    configure_logging,
    create_agent,
    log_context,
)
from boba.web.chainlit.config import ChainlitConfig, ChainlitSection


def _build_app_config(bundle: ConfigBundle) -> AppConfig:
    """Composition AppConfig из плоских секций приложения. Знание про
    конкретный набор адаптеров (workspaces/llm/prompts) живёт здесь, а
    не в фреймворке (см. ``boba.infra.config``: ConfigBundle generic)."""
    core = bundle.section(AppCoreSection)
    return AppConfig(
        workspaces=bundle.section(WorkspacesSection),
        llm=bundle.section(LLMTransportSection),
        prompts_dir=bundle.section(PromptsSection),
        ssl_verify=core.ssl_verify,
        log_level=core.log_level,
        log_file=core.log_file,
    )


class ChatSession:
    """One-shot обёртка: конфиг + workspace registry один раз, агент
    пересобирается на каждый run.

    Bundle инжектится из __main__ через
    set_bundle ДО первого cl.on_chat_start (там chainlit
    создаёт ChatSession() через functools.cache).
    """

    _bundle: ConfigBundle | None = None

    @classmethod
    def set_bundle(cls, bundle: ConfigBundle) -> None:
        """Инжектит application-level bundle. Должно быть вызвано до
        первого ChatSession()-вызова — обычно из __main__.main().
        """
        cls._bundle = bundle

    def __init__(self) -> None:
        if ChatSession._bundle is None:
            msg = (
                "ChatSession instantiated before ChatSession.set_bundle() — "
                "bootstrap must call set_bundle() in __main__.main()."
            )
            raise RuntimeError(msg)
        bundle = ChatSession._bundle
        self._app_config = _build_app_config(bundle)
        configure_logging(self._app_config.log_level, self._app_config.log_file)
        self._agent_config = bundle.section(AgentSection)
        self._chainlit_config: ChainlitConfig = bundle.section(ChainlitSection)

        self._workspaces = FsProjectWorkspaceRegistry(
            base_dir=Path(self._app_config.workspaces.base_dir),
            subdir=self._app_config.workspaces.user_subdir,
        )

        self._history_workspaces = FsHistoryWorkspaceRegistry(
            base_dir=Path(self._app_config.workspaces.base_dir),
            subdir=self._app_config.workspaces.system_subdir,
        )

        prompt_workspace = FsPromptWorkspaceRegistry(
            root=Path(self._app_config.prompts_dir),
        ).get_or_create(PromptWorkspaceId("prompts"))
        prompt_loader = PromptLoader(prompt_workspace)
        self._prompt_providers = prompt_loader.prompt_providers()

        tool_loader = ToolPluginLoader(ExtensionContext(config=bundle))
        self._tools_service = tool_loader.tools_service()

    @property
    def models(self) -> list[str]:
        """Список LLM-моделей для UI ChatSettings."""
        return self._chainlit_config.models

    def project_workspace(self, workspace_id: WorkspaceId) -> ProjectWorkspaceShell:
        """Project-workspace пользователя: тот же, куда смотрят file-tools агента.

        Registry живёт в инстансе ChatSession (APP scope) —
        shell доступен вне прогона агента, используется UI для
        upload/list/delete независимо от состояния цикла.
        """
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
        """Запустить агентский цикл. model обязателен и определяется
        только на стороне UI (ChatSettings) — конфиг в агентский луп не
        просачивается.

        extra_sink подмешивается к собранному source — это UI-мост
        (ChainlitBridgeSink).
        """
        # workspace подтягивается/создаётся, чтобы последующий upload в
        # тот же workspace_id работал; сам agent про него ничего не
        # знает — AgentRequest в boba workspace_id не хранит.
        project_workspace = self._workspaces.get_or_create(workspace_id)
        request_id = RequestId.new()

        # ToolContext — единственное per-request DI: прокидывает
        # сессионный workspace в Tool.execute через ToolExecutionMiddleware.
        # Сам ToolsService — application-singleton, собранный в __init__.
        tool_ctx = ToolContext(project_workspace=project_workspace)

        history_workspace = self._history_workspaces.get_or_create(workspace_id)
        observer = TranscriptChatCompletionObserver(history_workspace)
        llm_source = create_llm_source(self._app_config.llm, observer)
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
