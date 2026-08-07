"""Общий для всех пакетов setup: chainlit пишет служебные каталоги в APP_ROOT.

Импорт chainlit на уровне модуля создаёт `.chainlit/` и `.files/` в APP_ROOT,
а без переменной он равен cwd — голый `pytest` из корня засорял бы репозиторий.
"""

from __future__ import annotations

import os
from pathlib import Path

_APP_ROOT_ENV = "CHAINLIT_APP_ROOT"

_DEFAULT_APP_ROOT = Path(__file__).resolve().parents[1] / "compose" / "chainlit"

os.environ.setdefault(_APP_ROOT_ENV, str(_DEFAULT_APP_ROOT))
