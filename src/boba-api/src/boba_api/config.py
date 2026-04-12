"""Конфигурация FastAPI entry point.

Читает секцию [api] из BOBA_CONFIG, fallback на env vars.
"""
from __future__ import annotations

from dataclasses import dataclass

from boba_domain.config import _load_toml_section, _resolve


@dataclass(frozen=True)
class ApiConfig:
    """Настройки FastAPI сервера."""
    root_path: str = ""
    host: str = "0.0.0.0"
    port: int = 8000
    reload: bool = False

    @staticmethod
    def from_env() -> ApiConfig:
        toml = _load_toml_section("api")
        return ApiConfig(
            root_path=_resolve("API_ROOT_PATH", toml, "root_path", ""),
            host=_resolve("API_HOST", toml, "host", "0.0.0.0"),
            port=int(_resolve("API_PORT", toml, "port", "8000")),
            reload=_resolve("API_RELOAD", toml, "reload", "false").lower() in ("true", "1", "yes"),
        )
