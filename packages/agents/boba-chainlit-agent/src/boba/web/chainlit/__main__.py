"""Bootstrap chainlit-приложения."""

from __future__ import annotations

import os
import secrets
from collections.abc import Callable
from pathlib import Path
from typing import cast

from boba.agent.builder import AgentBuilder
from boba.agent.messages import InMemoryMessageService, MessageService
from boba.agent.workspace_fs import (
    FsHistoryWorkspaceRegistry,
    FsProjectWorkspaceRegistry,
)
from boba.web.chainlit.auth import (
    AuthenticateUser,
    StaticUserRepository,
)
from boba.web.chainlit.bootstrap import set_app_state
from boba.web.chainlit.config import ChainlitConfig
from boba.web.chainlit.data_layer import (
    BobaDataLayer,
    FsThreadRepository,
    FsUserCatalog,
    ThreadDerivedWorkspaceOwnership,
)
from boba.web.chainlit.infra import AppConfig, configure_logging
from boba.web.chainlit.session import ChatSession
from boba.web.chainlit.ui_overrides import UIOverrideTomlConverter
from boba.web.chainlit.usecase import ChatSessionPool, OpenChatSession
from boba.workspace.contract import (
    HistoryWorkspaceRegistry,
    HistoryWorkspaceShell,
    ProjectWorkspaceRegistry,
    WorkspaceId,
)

_SYSTEM_WORKSPACE_ID = WorkspaceId("_chainlit_system")
"""Workspace для системных файлов chainlit (users.json, threads-index.json)."""


def bridge_chainlit_env(cfg: ChainlitConfig) -> Path:
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


def write_ui_config_overrides(cfg: ChainlitConfig, app_root: Path) -> None:
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
    make_message_service: Callable[[WorkspaceId], MessageService],
) -> Callable[[WorkspaceId], ChatSession]:
    """Замыкание над deps; возвращает фабрику ChatSession по workspace_id."""

    def build(workspace_id: WorkspaceId) -> ChatSession:
        return ChatSession(
            workspace_id,
            make_builder(),
            project_workspaces,
            history_workspaces,
            make_message_service(workspace_id),
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

    bridge_chainlit_env(chainlit_cfg)
    write_ui_config_overrides(chainlit_cfg, app_root)

    workspaces_base = Path(app.workspaces.base_dir)
    project_workspaces = FsProjectWorkspaceRegistry(
        base_dir=workspaces_base,
        subdir=app.workspaces.user_subdir,
    )
    history_workspaces = FsHistoryWorkspaceRegistry(
        base_dir=workspaces_base,
        subdir=app.workspaces.system_subdir,
    )

    def make_message_service(_workspace_id: WorkspaceId) -> MessageService:
        # InMemory: контекст агента живёт в RAM ChatSession. Chainlit-уровень
        # отдельно персистит steps.jsonl через data_layer для UI/sidebar.
        return InMemoryMessageService()

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
            make_message_service,
        ),
        capacity=chainlit_cfg.chat_session_pool_capacity,
    )
    open_chat_session = OpenChatSession(chat_session_pool)

    system_shell = cast(
        "HistoryWorkspaceShell",
        history_workspaces.get_or_create(_SYSTEM_WORKSPACE_ID),
    )
    user_catalog = FsUserCatalog(system_shell)
    thread_repository = FsThreadRepository(
        history_workspaces=history_workspaces,
        system_shell=system_shell,
    )
    data_layer = BobaDataLayer(user_catalog, thread_repository)
    workspace_ownership = ThreadDerivedWorkspaceOwnership(thread_repository)

    set_app_state(
        authenticate_user,
        open_chat_session,
        data_layer,
        workspace_ownership,
    )

    # chainlit импортируется только после bootstrap — он читает env при загрузке.
    from chainlit.cli import run_chainlit  # noqa: PLC0415

    app_path = Path(__file__).with_name("app.py")
    run_chainlit(str(app_path))
    return 0


if __name__ == "__main__":
    main()
