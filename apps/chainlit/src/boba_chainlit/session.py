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

from pathlib import Path

from boba.adapters.fs_workspace import (
    FsHistoryWorkspaceRegistry,
    FsProjectWorkspaceRegistry,
    FsPromptWorkspaceRegistry,
)
from boba.adapters.in_memory_messages import InMemoryMessageService
from boba.adapters.raw_llm_observer import FileContentObserver
from boba.domain.agent.dialogue_writer import DialogueWriter
from boba.domain.agent.events import AgentEvent
from boba.domain.agent.meat.agent import Agent
from boba.domain.agent.models import AgentContext, AgentRequest
from boba.domain.core.patterns import StreamSink, StreamSinkPipeline
from boba.domain.core.tools import ToolContext
from boba.domain.core.workspace import (
    ProjectWorkspaceShell,
    PromptWorkspaceId,
    WorkspaceId,
)
from boba.domain.llm.models import RequestId
from boba.infra.config import ConfigLoader
from boba.infra.container import (
    AgentComponents,
    build_prompt_providers,
    create_agent_source,
    create_llm_source,
)
from boba.infra.logging import configure_logging, log_context
from boba.infra.prompt_loader import PromptLoader
from boba.infra.tool_plugin_loader import ExtensionContext, ToolPluginLoader


class ChatSession:
    """One-shot обёртка: конфиг + workspace registry один раз, агент
    пересобирается на каждый :meth:`run`.

    Chainlit держит один инстанс на процесс (через ``functools.cache``).
    Workspace-registry живёт снаружи агентского цикла и используется
    UI-слоем для upload'ов файлов (см. :meth:`project_workspace`).
    """

    def __init__(self) -> None:
        loader = ConfigLoader()
        self._app_config = loader.load_app()
        configure_logging(self._app_config.log_level, self._app_config.log_file)
        self._agent_config = loader.load_agent()
        self._workspaces = FsProjectWorkspaceRegistry(
            base_dir=Path(self._app_config.workspaces.base_dir),
            subdir=self._app_config.workspaces.user_subdir,
        )
        self._history_workspaces = FsHistoryWorkspaceRegistry(
            base_dir=Path(self._app_config.workspaces.base_dir),
            subdir=self._app_config.workspaces.system_subdir,
        )
        # Prompts: file-based discovery .md/.txt из BOBA_PROMPTS_DIR.
        # Tool-плагины: pip-installed Python-пакеты, регистрируемые через
        # entry-points группы "boba.tools". Оба loader'а application-
        # level: discovery + сборка происходят в конструкторе один раз
        # на всю жизнь процесса. ExtensionContext не несёт per-request
        # данных — tool'ы получают рабочий workspace через ToolContext
        # в execute, prompt-провайдеры — через state.ctx в blocks.
        prompt_workspace = FsPromptWorkspaceRegistry(
            root=Path(self._app_config.prompts_dir),
        ).get_or_create(PromptWorkspaceId("prompts"))
        prompt_loader = PromptLoader(prompt_workspace)
        self._prompt_providers = build_prompt_providers(prompt_loader)

        tool_loader = ToolPluginLoader(
            ExtensionContext(
                app_config=self._app_config,
                agent_config=self._agent_config,
            ),
        )
        self._tools_service = tool_loader.tools_service()

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
