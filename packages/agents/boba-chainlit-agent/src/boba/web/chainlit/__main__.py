"""Bootstrap chainlit-приложения."""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

from boba.agent.builder import AgentBuilder
from boba.agent.workspace_fs import (
    FsHistoryWorkspaceRegistry,
    FsProjectWorkspaceRegistry,
)
from boba.config.builder import ConfigBundleBuilder
from boba.config.bundle import ConfigBundle
from boba.config.source.toml import use_toml
from boba.web.chainlit.bootstrap import set_app_state
from boba.web.chainlit.config import ChainlitConfig
from boba.web.chainlit.infra import AppConfig
from boba.web.chainlit.ui_overrides import UIOverrideTomlConverter


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


def _build_bundle() -> ConfigBundle:
    """Собрать общий для всех сессий ConfigBundle."""
    return (
        ConfigBundleBuilder().use_cli().use_env_file().use_env().pipe(use_toml).build()
    )


def _make_builder_factory(bundle: ConfigBundle) -> Callable[[], AgentBuilder]:
    """Свежий builder под per-session ChatSession: общий bundle, свои plugins."""

    def factory() -> AgentBuilder:
        return AgentBuilder().use_config_bundle(bundle).use_tools_plugins_discovered()

    return factory


def main() -> int:
    bundle = _build_bundle()

    chainlit_cfg = bundle.get(ChainlitConfig, "chainlit")
    app = bundle.get(AppConfig, "agent")

    app_root = bridge_chainlit_env(chainlit_cfg)
    write_ui_config_overrides(chainlit_cfg, app_root)

    project_workspaces = FsProjectWorkspaceRegistry(
        base_dir=Path(app.workspaces.base_dir),
        subdir=app.workspaces.user_subdir,
    )
    history_workspaces = FsHistoryWorkspaceRegistry(
        base_dir=Path(app.workspaces.base_dir),
        subdir=app.workspaces.system_subdir,
    )

    # ChatSession создаётся лениво при первом cl.on_chat_start (см. app.py)
    # и получает свой свежий AgentBuilder через factory.
    set_app_state(
        _make_builder_factory(bundle),
        project_workspaces,
        history_workspaces,
    )

    # chainlit импортируется только после bootstrap — он читает env при загрузке.
    from chainlit.cli import run_chainlit  # noqa: PLC0415

    app_path = Path(__file__).with_name("app.py")
    run_chainlit(str(app_path))
    return 0


if __name__ == "__main__":
    main()
