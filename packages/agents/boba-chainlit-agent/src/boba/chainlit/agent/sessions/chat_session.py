"""Сборка агента и запуск одного запроса с UI-sink'ом."""

from __future__ import annotations

from boba.agent import AgentBuilder, TurnBuilder
from boba.agent.agent import Agent
from boba.agent.history import JsonLinesHistoryService
from boba.agent.prompt import PromptId
from boba.chainlit.agent.config import ChainlitConfig
from boba.chainlit.agent.logging import log_context
from boba.chainlit.agent.models import ThreadId
from boba.chainlit.agent.rendering.bridge import ChainlitBridgeSink
from boba.chainlit.agent.storage import ThreadRepository
from boba.chainlit.agent.system_prompt import (
    DefaultSystemPromptSource,
    ThreadSystemPromptProvider,
)
from boba.chainlit.agent.tool_cache import AvailableToolsCache
from boba.chainlit.agent.tool_filter import ThreadFilteredToolCatalog
from boba.llm.builder import LLMBuilder
from boba.provider.openai import (
    CurlTraceChatCompletionObserver,
    HttpTraceChatCompletionObserver,
    use_openai,
)
from boba.tools.framework import ToolCatalog
from boba.workspace.contract import (
    HistoryWorkspaceRegistry,
    HistoryWorkspaceShell,
    ProjectWorkspaceRegistry,
    ProjectWorkspaceShell,
    WorkspaceId,
)

__all__ = ["ChatSession"]


class ChatSession:
    """Per-workspace обёртка: один Agent, привязанный к конкретному workspace_id.

    `thread_id` фиксируется при сборке и используется `ThreadSystemPromptProvider`
    для чтения per-thread system-prompt на каждом turn'е без пересборки сессии.
    Инвариант workspace 1:1 thread держится chainlit-уровнем (см. ChatSessionPool).
    """

    def __init__(
        self,
        workspace_id: WorkspaceId,
        thread_id: ThreadId,
        builder: AgentBuilder,
        project_workspaces: ProjectWorkspaceRegistry,
        history_workspaces: HistoryWorkspaceRegistry,
        thread_repository: ThreadRepository,
        default_prompt_source: DefaultSystemPromptSource,
        available_tools: AvailableToolsCache,
    ) -> None:
        self._workspace_id = workspace_id
        self._thread_id = thread_id
        self._available_tools = available_tools
        self._thread_repository = thread_repository
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
            .build(use_openai(rt.openai))
        )

        turn = TurnBuilder(rt.model).system_prompt_from_providers(
            [
                ThreadSystemPromptProvider(
                    PromptId("thread_system_prompt"),
                    priority=100,
                    repository=thread_repository,
                    thread_id=thread_id,
                    fallback=default_prompt_source.read(),
                ),
            ]
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
            .decorate_tool_catalog(self._wrap_catalog)
            .build()
        )

    def _wrap_catalog(self, inner: ToolCatalog) -> ToolCatalog:
        """AgentBuilder-hook: кешируем discovered tools и вешаем per-thread фильтр.

        Кеш заполняется один раз — первый build выигрывает гонку и фиксирует
        набор tool-схем для UI. ThreadFilteredToolCatalog на каждом turn'е
        читает meta.enabled_tool_ids; вне списка — tool скрыт от LLM.
        """
        self._available_tools.set_once(list(inner.definitions()))
        return ThreadFilteredToolCatalog(
            inner,
            self._thread_repository,
            self._thread_id,
        )

    def project_workspace(self) -> ProjectWorkspaceShell:
        """Project-workspace сессии — тот же, куда смотрят file-tools агента."""
        return self._project_shell

    def run(self, query: str, extra_sink: ChainlitBridgeSink) -> None:
        """Запустить агентский цикл; модель задана при сборке."""
        with log_context(workspace_id=self._workspace_id):
            for event in self._agent.stream(query):
                extra_sink.handle(event)
