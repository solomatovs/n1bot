"""Чтение ``[chainlit]`` секции из ``BOBA_CONFIG``.

Используется и в :mod:`boba.chainlit.__main__` (host/port/auth_secret для
прокидывания в env до импорта chainlit), и в :mod:`boba.chainlit.app`
(список моделей для ChatSettings). Вынесено в отдельный модуль, чтобы
оба потребителя шли через одну реализацию.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import tomli


def load_chainlit_section() -> dict[str, Any]:
    """Вернуть ``[chainlit]`` секцию из TOML, указанного в ``BOBA_CONFIG``.

    Пустой словарь, если env-переменной нет, файл отсутствует или
    секция не задана. На исключения парсера не защищаемся — битый TOML
    должен падать громко.
    """
    path = os.environ.get("BOBA_CONFIG")
    if not path:
        return {}
    cfg = Path(path)
    if not cfg.is_file():
        return {}
    with cfg.open("rb") as f:
        data = tomli.load(f)
    section = data.get("chainlit")
    return section if isinstance(section, dict) else {}


def load_models() -> list[str]:
    """Список моделей для ChatSettings.

    TOML: ``[chainlit] models = ["qwen3.5-35b", "qwen3-0.6b", ...]``.
    Нестроковые элементы отбрасываются; пустой или отсутствующий список
    → ``[]`` (caller сам решает, что показать).
    """
    section = load_chainlit_section()
    raw = section.get("models")
    if not isinstance(raw, list):
        return []
    return [str(m) for m in raw if isinstance(m, str) and m]
