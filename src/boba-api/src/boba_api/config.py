"""Конфигурация FastAPI entry point.

Читает секцию [api] из BOBA_CONFIG, fallback на env vars.
"""

from __future__ import annotations

from dataclasses import dataclass

from boba_domain import toml_config


@dataclass(frozen=True)
class ApiConfig:
    """Настройки FastAPI сервера."""

    root_path: str = ""
    host: str = "0.0.0.0"
    port: int = 8000
    reload: bool = False

    @staticmethod
    def from_env() -> ApiConfig:
        toml = toml_config.load_section("api")
        return ApiConfig(
            root_path=toml_config.resolve("API_ROOT_PATH", toml, "root_path", ""),
            host=toml_config.resolve("API_HOST", toml, "host", "0.0.0.0"),
            port=int(toml_config.resolve("API_PORT", toml, "port", "8000")),
            reload=toml_config.resolve("API_RELOAD", toml, "reload", "false").lower()
            in ("true", "1", "yes"),
        )
