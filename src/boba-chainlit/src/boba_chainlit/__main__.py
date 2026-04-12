"""python -m boba_chainlit — запуск из конфига."""
import os
import sys
from pathlib import Path

from boba_chainlit.config import ChainlitConfig

cfg = ChainlitConfig.from_env()

# Chainlit читает эти env vars внутри run_chainlit()
os.environ.setdefault("CHAINLIT_HOST", cfg.host)
os.environ.setdefault("CHAINLIT_PORT", str(cfg.port))
os.environ.setdefault("CHAINLIT_ROOT_PATH", cfg.root_path)

from chainlit.cli import run_chainlit

target = str(Path(__file__).with_name("app.py"))
run_chainlit(target)
