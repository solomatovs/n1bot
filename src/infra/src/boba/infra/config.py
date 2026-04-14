"""Загрузка AppConfig из TOML-файла и переменных окружения.

Приоритет (от высшего к низшему):
    1. Env var <KEY>_FILE — путь к файлу с секретом
    2. Env var <KEY> — переменная окружения
    3. TOML-файл (секция [app]) — путь задаётся через BOBA_CONFIG
    4. Значение по умолчанию
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from boba.domain.config import AppConfig


class ConfigLoader:
    """Загружает AppConfig, разрешая значения из env / TOML / секретов."""

    def __init__(self, section: str = "app") -> None:
        self._toml = self._load_section(section)

    def load(self) -> AppConfig:
        return AppConfig(
            ssl_verify=self._resolve("SSL_VERIFY", "ssl_verify", "false").lower()
            in ("true", "1", "yes"),
            log_level=self._resolve("LOG_LEVEL", "log_level", "INFO"),
        )

    def _resolve(self, key: str, toml_key: str, default: str = "") -> str:
        file_path = os.environ.get(f"{key}_FILE")
        if file_path:
            p = Path(file_path)
            if p.is_file():
                return p.read_text(encoding="utf-8").strip()

        env_val = os.environ.get(key)
        if env_val is not None:
            return env_val

        toml_val = self._toml.get(toml_key)
        if toml_val is not None:
            return str(toml_val)

        return default

    @staticmethod
    def _load_section(section: str) -> dict[str, Any]:
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
