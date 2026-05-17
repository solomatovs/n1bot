"""Composition root: сборка зависимостей и запуск chainlit-сервера."""

from __future__ import annotations

import os
import secrets
from collections.abc import Callable
from pathlib import Path
from typing import cast

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
from boba.chainlit.agent.ui_overrides import UIOverrideTomlConverter
from boba.workspace.contract import (
    HistoryWorkspaceRegistry,
    HistoryWorkspaceShell,
    ProjectWorkspaceRegistry,
    WorkspaceId,
)

__all__ = ["main"]

_SYSTEM_WORKSPACE_ID = WorkspaceId("_chainlit_system")
"""Workspace для системных файлов chainlit (users.json, threads-index.json)."""


def _bridge_chainlit_env(cfg: ChainlitConfig) -> Path:
    """Прокидывает ChainlitConfig в CHAINLIT_* env; возвращает абсолютный app_root."""
    os.environ.setdefault("CHAINLIT_HOST", cfg.host)
    os.environ.setdefault("CHAINLIT_PORT", cfg.port)
    if cfg.root_path:
        os.environ.setdefault("CHAINLIT_ROOT_PATH", cfg.root_path)
    if cfg.auth_secret:
        os.environ.setdefault("CHAINLIT_AUTH_SECRET", cfg.auth_secret)
    os.environ.setdefault("CHAINLIT_HEADLESS", cfg.headless)
    app_root = Path(cfg.app_root).resolve()
    app_root.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("CHAINLIT_APP_ROOT", str(app_root))
    return app_root


def _write_ui_config_overrides(cfg: ChainlitConfig, app_root: Path) -> None:
    """Рендерит app_root/.chainlit/config.toml из UI-полей конфига."""
    content = UIOverrideTomlConverter().convert(cfg)
    if not content:
        return
    target = app_root / ".chainlit" / "config.toml"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def _make_builder_factory() -> Callable[[], AgentBuilder]:
    """Свежий builder под per-session ChatSession (свои plugins)."""

    def factory() -> AgentBuilder:
        return AgentBuilder().use_tools_plugins_discovered()

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


def _resolve_auth_secret(configured: str | None, local_dir: Path) -> str:
    """Конфиг приоритетнее; иначе читаем/создаём local/.auth_secret (0600)."""
    if configured:
        return configured
    path = local_dir / ".auth_secret"
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    local_dir.mkdir(parents=True, exist_ok=True)
    secret = secrets.token_urlsafe(32)
    path.write_text(secret + "\n", encoding="utf-8")
    path.chmod(0o600)
    return secret


def main() -> int:
    chainlit_cfg = ChainlitConfig.load()
    app = AppConfig.load()

    configure_logging(app.core.log_level, app.core.log_file)

    app_root = Path(chainlit_cfg.app_root).resolve()
    auth_secret = _resolve_auth_secret(chainlit_cfg.auth_secret, app_root.parent)
    os.environ["CHAINLIT_AUTH_SECRET"] = auth_secret

    _bridge_chainlit_env(chainlit_cfg)
    _write_ui_config_overrides(chainlit_cfg, app_root)

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
        shell = cast(
            "HistoryWorkspaceShell",
            history_workspaces.get_or_create(workspace_id),
        )
        return JsonLinesHistoryService(shell)

    user_repository = StaticUserRepository(
        {chainlit_cfg.auth_username: chainlit_cfg.auth_password},
    )
    authenticate_user = AuthenticateUser(user_repository)

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

    system_shell = cast(
        "HistoryWorkspaceShell",
        history_workspaces.get_or_create(_SYSTEM_WORKSPACE_ID),
    )
    user_catalog = FsUserCatalog(system_shell)
    thread_repository = FsThreadRepository(system_shell=system_shell)
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
