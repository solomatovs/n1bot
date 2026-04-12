"""Прокидывание [chainlit] секции из config.toml в env vars.

Chainlit читает CHAINLIT_HOST/PORT/ROOT_PATH из env при первом импорте.
Вызовите init() ДО любых импортов из chainlit.
"""
import os

from boba_chainlit.config import ChainlitConfig


def init() -> None:
    cfg = ChainlitConfig.from_env()
    os.environ.setdefault("CHAINLIT_HOST", cfg.host)
    os.environ.setdefault("CHAINLIT_PORT", str(cfg.port))
    os.environ.setdefault("CHAINLIT_ROOT_PATH", cfg.root_path)
