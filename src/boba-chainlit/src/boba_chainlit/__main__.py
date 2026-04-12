"""python -m boba_chainlit — запуск из config.toml."""

from pathlib import Path

from boba_chainlit._bootstrap import init
from chainlit.cli import run_chainlit

init()
run_chainlit(str(Path(__file__).with_name("app.py")))
