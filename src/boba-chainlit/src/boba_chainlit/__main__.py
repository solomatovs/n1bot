"""python -m boba_chainlit — запуск из config.toml.

Chainlit читает host/port/root_path из env vars (CHAINLIT_HOST и т.д.).
Мы загружаем [chainlit] секцию из BOBA_CONFIG и прокидываем значения
в env перед запуском — это единственный способ передать их в run_chainlit().
"""
import os
from pathlib import Path

from boba_chainlit.config import ChainlitConfig

cfg = ChainlitConfig.from_env()

os.environ.setdefault("CHAINLIT_HOST", cfg.host)
os.environ.setdefault("CHAINLIT_PORT", str(cfg.port))
os.environ.setdefault("CHAINLIT_ROOT_PATH", cfg.root_path)

from chainlit.cli import run_chainlit  # noqa: E402 — после setdefault

run_chainlit(str(Path(__file__).with_name("app.py")))
