"""Утилиты для чтения TOML-конфигурации.

Предоставляет функции загрузки секций и разрешения значений
с приоритетом: env file -> env var -> TOML -> default.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any


def load_section(section: str) -> dict[str, Any]:
    """Загрузить секцию из TOML-файла конфигурации.

    Путь к файлу задаётся через BOBA_CONFIG.
    Если файл не найден — возвращает пустой dict (все значения из defaults).
    """
    config_path = os.environ.get("BOBA_CONFIG", "")
    if not config_path:
        return {}
    path = Path(config_path)
    if not path.is_file():
        return {}
    try:
        import tomli

        with open(path, "rb") as f:
            data = tomli.load(f)
        return data.get(section, {})
    except Exception:
        return {}


def resolve(
    key: str, toml_data: dict[str, Any], toml_key: str, default: str = ""
) -> str:
    """Получить значение конфигурации.

    Приоритет:
    1. <KEY>_FILE — путь к файлу с секретом
    2. <KEY> — env var
    3. TOML [section].<toml_key>
    4. default
    """
    file_path = os.environ.get(f"{key}_FILE")
    if file_path:
        p = Path(file_path)
        if p.is_file():
            return p.read_text(encoding="utf-8").strip()
    env_val = os.environ.get(key)
    if env_val is not None:
        return env_val
    toml_val = toml_data.get(toml_key)
    if toml_val is not None:
        return str(toml_val)
    return default
