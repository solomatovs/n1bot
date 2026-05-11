"""Bootstrap chainlit-приложения."""

from __future__ import annotations

import os
from pathlib import Path

from boba.agent.builder import AgentBuilder
from boba.agent.workspace_fs import (
    FsHistoryWorkspaceRegistry,
    FsProjectWorkspaceRegistry,
)
from boba.patterns import ConverterInputError
from boba.web.chainlit.config import ChainlitConfig
from boba.web.chainlit.infra import AppConfig, use_toml_config
from boba.web.chainlit.session import ChatSession
from boba.web.chainlit.ui_overrides import UIOverrideTomlConverter
from boba.workspace.contract import (
    HistoryWorkspaceRegistry,
    ProjectWorkspaceRegistry,
)


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


def main() -> int:
    builder = (
        AgentBuilder()
        .use_cli()
        .use_env_file()
        .use_env()
        .pipe(use_toml_config)
    )
    try:
        chainlit_cfg = builder.bundle().get(ChainlitConfig, "chainlit")
        app = builder.bundle().get(AppConfig, "agent")
    except ConverterInputError:
        return 2
    app_root = bridge_chainlit_env(chainlit_cfg)
    write_ui_config_overrides(chainlit_cfg, app_root)

    # Registry'и нужны двум потребителям: ToolExecutor (через ExtensionContext)
    # и ChatSession (для project_workspace() и history-observer'ов).
    project_workspaces = FsProjectWorkspaceRegistry(
        base_dir=Path(app.workspaces.base_dir),
        subdir=app.workspaces.user_subdir,
    )
    history_workspaces = FsHistoryWorkspaceRegistry(
        base_dir=Path(app.workspaces.base_dir),
        subdir=app.workspaces.system_subdir,
    )
    builder = (
        builder
        .with_extension(ProjectWorkspaceRegistry, project_workspaces)
        .with_extension(HistoryWorkspaceRegistry, history_workspaces)
        .use_tools_plugins_discovered()
    )

    # ChatSession создаётся лениво при первом cl.on_chat_start.
    ChatSession.set_builder(
        builder,
        project_workspaces=project_workspaces,
        history_workspaces=history_workspaces,
    )

    # chainlit импортируется только после bootstrap — он читает env при загрузке.
    from chainlit.cli import run_chainlit  # noqa: PLC0415

    app_path = Path(__file__).with_name("app.py")
    run_chainlit(str(app_path))
    return 0


if __name__ == "__main__":
    main()
