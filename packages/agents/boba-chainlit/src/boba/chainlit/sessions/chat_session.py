"""Сборка агента и запуск одного запроса с UI-sink'ом."""

from __future__ import annotations

from pathlib import Path

from boba.agent import (
    AgentBuilder,
    CompactHistoryDialogView,
    LLMPort,
    TurnBuilder,
)
from boba.agent.agent import Agent
from boba.agent.history import HistoryService, JsonLinesHistoryService
from boba.agent.prompt import PromptId
from boba.agent.workspace_fs import FsWorkspaceShell
from boba.chainlit.config import ChainlitConfig
from boba.chainlit.logging import log_context
from boba.chainlit.models import ThreadId
from boba.chainlit.rendering.bridge import ChainlitBridgeSink
from boba.chainlit.storage import ThreadRepository
from boba.chainlit.system_prompt import (
    DefaultSystemPromptSource,
    ThreadSystemPromptProvider,
)
from boba.chainlit.tool_cache import AvailableToolsCache
from boba.chainlit.tool_filter import ThreadFilteredToolCatalog
from boba.llm.builder import LLMBuilder
from boba.provider.openai import (
    CurlTraceChatCompletionObserver,
    HttpTraceChatCompletionObserver,
    use_openai,
)
from boba.settings import bind, build_app_config
from boba.tools import ToolBuilder
from boba.tools.framework import ToolRegistry
from boba.workspace.contract import (
    ProjectWorkspaceShell,
    WorkspaceId,
    WorkspaceShell,
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
        tool_builder: ToolBuilder,
        project_base_dir: Path,
        history_base_dir: Path,
        thread_repository: ThreadRepository,
        default_prompt_source: DefaultSystemPromptSource,
        available_tools: AvailableToolsCache,
    ) -> None:
        self._workspace_id = workspace_id
        self._thread_id = thread_id
        self._available_tools = available_tools
        self._thread_repository = thread_repository
        self._chainlit_config = bind(build_app_config(), "chainlit", ChainlitConfig)
        rt = self._chainlit_config.profile

        # Свежий shell на сессию (project) и на каждую history-службу:
        # общий root, но изолированное состояние (cwd) у каждого потребителя.
        self._project_shell = FsWorkspaceShell.under(project_base_dir, workspace_id)

        def history_shell() -> FsWorkspaceShell[WorkspaceId]:
            return FsWorkspaceShell.under(history_base_dir, workspace_id)

        llm = (
            LLMBuilder()
            .add_observer(CurlTraceChatCompletionObserver(history_shell()))
            .add_observer(HttpTraceChatCompletionObserver(history_shell()))
            .build(use_openai(rt.openai))
        )

        self._tool_registry: ToolRegistry = tool_builder.register_instance(
            self._project_shell,
            provides=ProjectWorkspaceShell,
        ).build()

        catalog = self._wrap_catalog(self._tool_registry.catalog())

        history: HistoryService = JsonLinesHistoryService(history_shell())

        turn = (
            TurnBuilder(rt.model)
            .system_prompt_from_providers(
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
            .with_history_view(
                CompactHistoryDialogView(history, max_messages=rt.max_messages),
            )
            .with_tool_catalog(catalog)
            .with_stream(rt.stream)
        )

        terminal = LLMPort(llm, turn)

        self._agent: Agent = (
            AgentBuilder()
            .use_history(history)
            .use_tool_executor(self._tool_registry.executor())
            .build(terminal)
        )

    def _wrap_catalog(self, inner):
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

    def project_workspace(self) -> WorkspaceShell:
        """Project-workspace сессии — тот же, куда смотрят file-tools агента."""
        return self._project_shell

    def run(self, query: str, extra_sink: ChainlitBridgeSink) -> None:
        """Запустить агентский цикл; модель задана при сборке."""
        with log_context(workspace_id=self._workspace_id):
            for event in self._agent.stream(query):
                extra_sink.handle(event)
