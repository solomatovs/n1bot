"""Сборка агента и запуск одного запроса с UI-sink'ом.

В отличие от старой версии (на Dishka-контейнере и ``request_scope``)
эта реализация не тянет DI-инфраструктуру — ``boba`` её не использует.
Собирается вручную: один раз в конструкторе — workspace registry и
конфиг; на каждый ``run`` — свежий source через
:func:`create_agent_source`, ``Agent(source, UI-sink)``, синхронный
прогон.

Один инстанс на процесс Chainlit: чтобы не перечитывать конфиг и не
пересоздавать workspace registry на каждое сообщение. Состояние
сессии (``WorkspaceId``) живёт в ``cl.user_session``.
"""

from __future__ import annotations

import os
from pathlib import Path

from boba.domain.agent import Agent
from boba.domain.agent.dialogue_writer import DialogueWriter
from boba.domain.agent.events import AgentEvent
from boba.domain.agent.models import AgentContext, AgentRequest
from boba.domain.core.config import ChainedConfigResolver
from boba.domain.core.patterns import StreamSink, StreamSinkPipeline
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
    ConfigFactory,
    ConfigLoader,
    ExtensionContext,
    ToolPluginLoader,
    configure_logging,
    create_agent_source,
    log_context,
)
from boba_adapter_fs_workspace import (
    FsHistoryWorkspaceRegistry,
    FsProjectWorkspaceRegistry,
    FsPromptWorkspaceRegistry,
    WorkspacesSection,
)
from boba_adapter_messages import InMemoryMessageService
from boba_adapter_openai import (
    FileContentObserver,
    LLMTransportSection,
    create_llm_source,
)
from boba_adapter_prompt_providers import PromptLoader, PromptsSection
from boba_chainlit.config import ChainlitConfig, ChainlitSection
from boba_config_env import EnvFileSource, EnvSource
from boba_config_toml import (
    CONFIG_PATH_ENV,
    TomlFileSource,
    TomlSource,
    load_toml,
)


def _build_resolver() -> ChainedConfigResolver:
    """Стандартная цепочка для chainlit-runtime: env (с file-указателем) +
    TOML (с file-указателем). Тот же набор источников, что и у CLI —
    перечислено явно, чтобы было видно, какие подключены.
    """
    toml_data = load_toml(os.environ.get(CONFIG_PATH_ENV))
    return ChainedConfigResolver(
        [
            EnvFileSource(),
            EnvSource(),
            TomlFileSource(toml_data),
            TomlSource(toml_data),
        ]
    )


def _build_factory() -> ConfigFactory:
    """Регистрирует встроенные секции (``app_core``/``agent``) и
    adapter-секции выбранного стека (FS-workspace, OpenAI-транспорт,
    file-prompt loader). Расширения через entry-point group
    ``boba.config_sections`` подхватываются после.
    """
    factory = ConfigFactory(_build_resolver())
    factory.register(AppCoreSection())
    factory.register(AgentSection())
    factory.register(WorkspacesSection())
    factory.register(LLMTransportSection())
    factory.register(PromptsSection())
    factory.discover_extension_sections()
    return factory


class ChatSession:
    """One-shot обёртка: конфиг + workspace registry один раз, агент
    пересобирается на каждый :meth:`run`.

    Chainlit держит один инстанс на процесс (через ``functools.cache``).
    Workspace-registry живёт снаружи агентского цикла и используется
    UI-слоем для upload'ов файлов (см. :meth:`project_workspace`).
    """

    def __init__(self) -> None:
        loader = ConfigLoader(_build_factory())
        bundle = loader.load_bundle()
        self._app_config = bundle.app
        configure_logging(self._app_config.log_level, self._app_config.log_file)
        self._agent_config = bundle.agent
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

        Registry живёт в инстансе :class:`ChatSession` (APP scope) —
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
        """Запустить агентский цикл. ``model`` обязателен и определяется
        только на стороне UI (ChatSettings) — конфиг в агентский луп не
        просачивается.

        ``extra_sink`` подмешивается к собранному source — это UI-мост
        (:class:`~boba_chainlit.bridge.ChainlitBridgeSink`).
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
        observer = FileContentObserver(history_workspace)
        message_service = InMemoryMessageService()
        writer = DialogueWriter(message_service)
        llm_source = create_llm_source(self._app_config.llm, observer)
        source = create_agent_source(
            llm_source,
            AgentComponents(
                agent_config=self._agent_config,
                prompt_providers=self._prompt_providers,
                message_service=message_service,
                tools_service=self._tools_service,
            ),
            tool_ctx,
            writer,
        )
        sink = StreamSinkPipeline([extra_sink])
        agent = Agent(source=source, sink=sink, writer=writer)

        request = AgentRequest(
            model=model,
            request_id=request_id,
        )
        with log_context(
            request_id=request_id.to_wire(),
            workspace_id=workspace_id.to_wire(),
        ):
            agent.run(self._agent_config, request, query)
