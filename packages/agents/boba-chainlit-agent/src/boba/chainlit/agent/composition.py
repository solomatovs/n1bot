"""Composition root: сборка зависимостей и запуск chainlit-сервера."""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

from boba.agent.builder import AgentBuilder
from boba.agent.history import HistoryService, JsonLinesHistoryService
from boba.agent.workspace_fs import (
    FsHistoryWorkspaceRegistry,
    FsProjectWorkspaceRegistry,
)
from boba.chainlit.agent.auth import AuthenticateUser, StaticUserRepository
from boba.chainlit.agent.config import AppConfig, ChainlitConfig
from boba.chainlit.agent.data_layer import BobaDataLayer
from boba.chainlit.agent.logging import configure_logging
from boba.chainlit.agent.sessions import (
    ChatSession,
    ChatSessionPool,
    OpenChatSession,
)
from boba.chainlit.agent.state import set_app_state
from boba.chainlit.agent.storage import FsThreadRepository, FsUserCatalog
from boba.workspace.contract import (
    HistoryWorkspaceRegistry,
    ProjectWorkspaceRegistry,
    WorkspaceId,
)

__all__ = ["main"]

_SYSTEM_WORKSPACE_ID = WorkspaceId("_chainlit_system")
"""Workspace для системных файлов chainlit (users.json, threads-index.json)."""


def _bridge_chainlit_env(cfg: ChainlitConfig) -> None:
    """Прокидывает ChainlitConfig в CHAINLIT_* env.

    DTO — единственный источник истины: значения env жёстко перезаписываются,
    чтобы случайно унаследованный CHAINLIT_* из окружения не маскировал конфиг.
    """
    os.environ["CHAINLIT_HOST"] = cfg.host
    os.environ["CHAINLIT_PORT"] = str(cfg.port)
    os.environ["CHAINLIT_ROOT_PATH"] = cfg.root_path
    os.environ["CHAINLIT_AUTH_SECRET"] = cfg.auth_secret
    os.environ["CHAINLIT_HEADLESS"] = "true" if cfg.headless else "false"
    app_root = Path(cfg.app_root).resolve()
    app_root.mkdir(parents=True, exist_ok=True)
    os.environ["CHAINLIT_APP_ROOT"] = str(app_root)


def _make_builder_factory() -> Callable[[], AgentBuilder]:
    """Свежий builder под per-session ChatSession (свои plugins).

    `use_plugins()` подцепляет v2-плагины через entry-points group
    `boba.plugins`. Пока v2-плагины не созданы — это no-op; будут
    появляться по мере миграции старых плагинов.
    """

    def factory() -> AgentBuilder:
        return AgentBuilder().use_plugins()

    return factory


def _make_chat_session_builder(
    make_builder: Callable[[], AgentBuilder],
    project_workspaces: ProjectWorkspaceRegistry,
    history_workspaces: HistoryWorkspaceRegistry,
    make_history_service: Callable[[WorkspaceId], HistoryService],
) -> Callable[[WorkspaceId], ChatSession]:
    """Замыкание над deps; возвращает фабрику ChatSession по workspace_id."""

    def build(workspace_id: WorkspaceId) -> ChatSession:
        return ChatSession(
            workspace_id,
            make_builder(),
            project_workspaces,
            history_workspaces,
            make_history_service(workspace_id),
        )

    return build


def main() -> int:
    chainlit_cfg = ChainlitConfig.load()
    app = AppConfig.load()

    configure_logging(app.core.log_level, app.core.log_file)

    _bridge_chainlit_env(chainlit_cfg)

    workspaces_base = Path(app.workspaces.base_dir)
    project_workspaces = FsProjectWorkspaceRegistry(
        base_dir=workspaces_base,
        subdir=app.workspaces.user_subdir,
    )
    history_workspaces = FsHistoryWorkspaceRegistry(
        base_dir=workspaces_base,
        subdir=app.workspaces.system_subdir,
    )

    def make_history_service(workspace_id: WorkspaceId) -> HistoryService:
        """JSONL per-workspace: chainlit и агент видят один и тот же журнал."""
        return JsonLinesHistoryService(history_workspaces.get_or_create(workspace_id))

    authenticate_user = AuthenticateUser(
        StaticUserRepository({"admin": "admin"})
    )

    builder_factory = _make_builder_factory()
    chat_session_pool = ChatSessionPool(
        _make_chat_session_builder(
            builder_factory,
            project_workspaces,
            history_workspaces,
            make_history_service,
        ),
        capacity=chainlit_cfg.chat_session_pool_capacity,
    )
    open_chat_session = OpenChatSession(chat_session_pool)

    system_shell = history_workspaces.get_or_create(_SYSTEM_WORKSPACE_ID)
    user_catalog = FsUserCatalog(system_shell)
    thread_repository = FsThreadRepository(system_shell)
    data_layer = BobaDataLayer(
        user_catalog,
        thread_repository,
        make_history_service,
    )

    set_app_state(
        authenticate_user,
        open_chat_session,
        data_layer,
        thread_repository,
    )

    # chainlit импортируется только после bootstrap — он читает env при загрузке.
    from chainlit.cli import run_chainlit  # noqa: PLC0415

    callbacks_path = Path(__file__).with_name("callbacks.py")
    run_chainlit(str(callbacks_path))
    return 0
