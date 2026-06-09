"""Composition root: сборка зависимостей и запуск chainlit-сервера."""

from __future__ import annotations

import logging
import os
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from boba.agent.history import HistoryService, JsonLinesHistoryService
from boba.agent.tool_config import (
    OmegaConfPluginToolFilter,
    OmegaConfResolver,
    bind,
    build_app_config,
)
from boba.agent.workspace_fs import FsWorkspaceShell
from boba.chainlit.auth import AuthenticateUser, StaticUserRepository
from boba.chainlit.config import ChainlitConfig
from boba.chainlit.data_layer import BobaDataLayer
from boba.chainlit.logging import configure_logging
from boba.chainlit.models import ThreadId
from boba.chainlit.sessions import (
    ChatSession,
    ChatSessionPool,
    OpenChatSession,
)
from boba.chainlit.settings import (
    PromptSection,
    SettingsSection,
    ToolsSection,
)
from boba.chainlit.state import set_app_state
from boba.chainlit.storage import (
    FsThreadRepository,
    FsUserCatalog,
    ThreadRepository,
)
from boba.chainlit.system_prompt import DefaultSystemPromptSource
from boba.chainlit.tool_cache import AvailableToolsCache
from boba.tools import ToolBuilder
from boba.workspace.contract import WorkspaceId

__all__ = ["main"]

_logger = logging.getLogger(__name__)

_SYSTEM_WORKSPACE_ID = WorkspaceId("_chainlit_system")
"""Workspace для системных файлов chainlit (users.json, threads-index.json)."""


def _bridge_chainlit_env(cfg: ChainlitConfig) -> None:
    """Прокидывает ChainlitConfig в CHAINLIT_* env.

    DTO — единственный источник истины: значения env жёстко перезаписываются,
    чтобы случайно унаследованный CHAINLIT_* из окружения не маскировал конфиг.
    """
    os.environ["CHAINLIT_HOST"] = cfg.host
    os.environ["CHAINLIT_PORT"] = str(cfg.port)
    os.environ["CHAINLIT_ROOT_PATH"] = cfg.url_prefix
    os.environ["CHAINLIT_AUTH_SECRET"] = cfg.auth_secret
    os.environ["CHAINLIT_HEADLESS"] = "true" if cfg.headless else "false"
    app_root = Path(cfg.app_root).resolve()
    app_root.mkdir(parents=True, exist_ok=True)
    os.environ["CHAINLIT_APP_ROOT"] = str(app_root)


def _make_tool_builder_factory(config: Any) -> Callable[[], ToolBuilder]:
    """Свежий ToolBuilder под per-session ChatSession (свои plugins).

    Резолвер/фильтр строятся вокруг переданного конфиг-инстанса (config
    собран один раз в main), поэтому per-session здесь — только ToolBuilder.

    discover_plugins() подцепляет v2-плагины через entry-points group
    boba.plugins. Пока v2-плагины не созданы — это no-op; будут
    появляться по мере миграции старых плагинов.
    """

    def factory() -> ToolBuilder:
        return (
            ToolBuilder()
            .use_config_resolver(OmegaConfResolver(config))
            .discover_plugins("boba.plugins", OmegaConfPluginToolFilter(config))
        )

    return factory


def _make_chat_session_builder(
    make_tool_builder: Callable[[], ToolBuilder],
    project_base_dir: Path,
    history_base_dir: Path,
    thread_repository: ThreadRepository,
    default_prompt_source: DefaultSystemPromptSource,
    available_tools: AvailableToolsCache,
) -> Callable[[WorkspaceId, ThreadId], ChatSession]:
    """Замыкание над deps; возвращает фабрику ChatSession по (workspace, thread)."""

    def build(workspace_id: WorkspaceId, thread_id: ThreadId) -> ChatSession:
        return ChatSession(
            workspace_id,
            thread_id,
            make_tool_builder(),
            project_base_dir,
            history_base_dir,
            thread_repository,
            default_prompt_source,
            available_tools,
        )

    return build


def _warm_tool_cache(
    chat_session_builder: Callable[[WorkspaceId, ThreadId], ChatSession],
) -> None:
    """Сборкой одноразовой ChatSession наполняем AvailableToolsCache.

    ChatSession.__init__ строит catalog и через _wrap_catalog
    зовёт set_once; повторный set_once на реальной сессии — no-op
    (контракт set-once). Если сборка падает — лог + продолжаем: tools
    потом подтянутся при первом on_message.
    """
    warm_thread = ThreadId(f"warmup-{uuid.uuid4()}")
    try:
        chat_session_builder(_SYSTEM_WORKSPACE_ID, warm_thread)
    except Exception:
        _logger.exception(
            "warmup ChatSession failed; tool checkboxes will appear after "
            "the first user message",
        )


def main() -> int:
    config = build_app_config()
    chainlit_cfg = bind(config, "chainlit", ChainlitConfig)
    rt = chainlit_cfg.profile

    configure_logging(rt.log_level, rt.log_file)

    _bridge_chainlit_env(chainlit_cfg)

    project_base_dir = Path(rt.user_workspace_dir)
    history_base_dir = Path(rt.system_workspace_dir)

    def make_history_service(workspace_id: WorkspaceId) -> HistoryService:
        """JSONL per-workspace: chainlit и агент видят один и тот же журнал."""
        return JsonLinesHistoryService(
            FsWorkspaceShell.under(history_base_dir, workspace_id)
        )

    authenticate_user = AuthenticateUser(
        StaticUserRepository(
            {
                "user1": "1974",
                "user2": "1974",
                "user3": "1974",
                "user4": "1974",
                "user5": "1974",
                "user6": "1974",
            }
        )
    )

    # Свежий shell на каждую службу: общий root, но изолированное состояние.
    user_catalog = FsUserCatalog(
        FsWorkspaceShell.under(history_base_dir, _SYSTEM_WORKSPACE_ID)
    )
    thread_repository = FsThreadRepository(
        FsWorkspaceShell.under(history_base_dir, _SYSTEM_WORKSPACE_ID)
    )
    default_prompt_source = DefaultSystemPromptSource(Path(rt.system_prompt_dir))
    available_tools = AvailableToolsCache()

    tool_builder_factory = _make_tool_builder_factory(config)
    chat_session_builder = _make_chat_session_builder(
        tool_builder_factory,
        project_base_dir,
        history_base_dir,
        thread_repository,
        default_prompt_source,
        available_tools,
    )
    chat_session_pool = ChatSessionPool(
        chat_session_builder,
        capacity=chainlit_cfg.chat_session_pool_capacity,
    )
    open_chat_session = OpenChatSession(chat_session_pool)

    # Eager-прогрев AvailableToolsCache: до первого on_chat_start cache пуст
    # -> ToolsSection отдаёт пустой список -> в шестерёнке нет таба «tools».
    # Одноразовая ChatSession через system workspace выполняет
    # ToolBuilder.discover_plugins + сборку catalog'а, что через
    # _wrap_catalog триггерит set_once. Сессия и её агент не
    # используются и сразу gc'ятся; workspace dir тот же, что под
    # users.json / threads-index.json, новых каталогов на диске не появляется.
    _warm_tool_cache(chat_session_builder)

    data_layer = BobaDataLayer(
        user_catalog,
        thread_repository,
        make_history_service,
    )

    # Реестр секций шестерёнки. Порядок = порядок табов слева направо.
    sections: tuple[SettingsSection, ...] = (
        PromptSection(default_prompt_source),
        ToolsSection(available_tools),
    )

    set_app_state(
        authenticate_user,
        open_chat_session,
        data_layer,
        thread_repository,
        default_prompt_source,
        available_tools,
        sections,
    )

    # chainlit импортируется только после bootstrap — он читает env при загрузке.
    from chainlit.cli import run_chainlit  # noqa: PLC0415

    callbacks_path = Path(__file__).with_name("callbacks.py")
    run_chainlit(str(callbacks_path))
    return 0
