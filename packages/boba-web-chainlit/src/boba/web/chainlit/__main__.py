"""Bootstrap chainlit-приложения."""

from __future__ import annotations

import os
from pathlib import Path

from boba.config.bundle import ConfigBundle
from boba.config.path import ConfigSource
from boba.config.source.cli import CliSource
from boba.config.source.env import EnvFileSource, EnvSource
from boba.config.source.toml import TomlFileSource, TomlSource
from boba.patterns import ConverterInputError
from boba.web.chainlit.config import ChainlitConfig
from boba.web.chainlit.session import ChatSession
from boba.web.chainlit.ui_overrides import UIOverrideTomlConverter


def _config_sources() -> list[ConfigSource]:
    return [
        CliSource(),
        EnvFileSource(),
        EnvSource(),
        TomlFileSource(),
        TomlSource(),
    ]


def build_app_config() -> ConfigBundle:
    """ConfigBundle для core-DTO + Plugin-протокола."""
    return ConfigBundle.from_sources(_config_sources())


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
        bundle = build_app_config()
    except ConverterInputError as _e:
        return 2
    chainlit_cfg = bundle.get(ChainlitConfig, "chainlit")
    app_root = bridge_chainlit_env(chainlit_cfg)
    write_ui_config_overrides(chainlit_cfg, app_root)

    # ChatSession создаётся лениво при первом cl.on_chat_start.
    ChatSession.set_bundle(bundle)

    # chainlit импортируется только после bootstrap — он читает env при загрузке.
    from chainlit.cli import run_chainlit  # noqa: PLC0415

    app_path = Path(__file__).with_name("app.py")
    run_chainlit(str(app_path))
    return 0


if __name__ == "__main__":
    main()
