"""Общая настройка для тестов (pytest conftest)."""
from __future__ import annotations

import sys
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
warnings.filterwarnings("ignore")

from env_loader import setup  # noqa: E402

setup()
