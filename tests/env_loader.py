"""Загрузка переменных окружения для тестов."""
from __future__ import annotations

import os
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _load_env_file(path: Path) -> None:
    """Загрузить переменные из .env файла (без перезаписи существующих)."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"')
        os.environ.setdefault(key, val)


def setup() -> None:
    """Загрузить .env.debug и secrets.toml в переменные окружения."""
    _load_env_file(_PROJECT_ROOT / "docker" / "env" / ".env.debug")
    _load_env_file(_PROJECT_ROOT / "docker" / ".streamlit" / "secrets.toml")
