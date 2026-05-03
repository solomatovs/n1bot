"""Bootstrap chainlit-приложения."""

from __future__ import annotations

import os
from pathlib import Path

from boba.adapter.fs_workspace import WorkspacesSection
from boba.adapter.openai import LLMTransportSection
from boba.adapter.prompt_providers import PromptsSection
from boba.config.app import AppConfig
from boba.config.bootstrap import AppConfigBootstrap
from boba.config.source.cli import CliSource
from boba.config.source.env import EnvFileSource, EnvSource
from boba.config.source.toml import TomlFileSource, TomlSource
from boba.patterns import ConverterInputError
from boba.web.chainlit.config import ChainlitConfig, ChainlitSection
from boba.web.chainlit.infra import AgentSection, AppCoreSection
from boba.web.chainlit.session import ChatSession
from boba.web.chainlit.ui_overrides import UIOverrideTomlConverter


def build_app_config() -> AppConfig:
    """AppConfig с зарегистрированными секциями и источниками (CLI > env > TOML)."""
    boot = AppConfigBootstrap()
    boot.register_section(AppCoreSection())
    boot.register_section(AgentSection())
    boot.register_section(WorkspacesSection())
    boot.register_section(LLMTransportSection())
    boot.register_section(PromptsSection())
    boot.register_section(ChainlitSection())
    boot.discover_extension_sections()
    boot.attach_sources(
        [
            CliSource(),
            EnvFileSource(),
            EnvSource(),
            TomlFileSource(),
            TomlSource(),
        ]
    )
    return boot.build()


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
    try:
        app = build_app_config()
    except ConverterInputError as _e:
        return 2
    chainlit_cfg = app.section(ChainlitSection)
    app_root = bridge_chainlit_env(chainlit_cfg)
    write_ui_config_overrides(chainlit_cfg, app_root)

    # ChatSession создаётся лениво при первом cl.on_chat_start.
    ChatSession.set_app(app)

    # chainlit импортируется только после bootstrap — он читает env при загрузке.
    from chainlit.cli import run_chainlit  # noqa: PLC0415

    app_path = Path(__file__).with_name("app.py")
    run_chainlit(str(app_path))
    return 0


if __name__ == "__main__":
    main()
