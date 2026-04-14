"""Конфигурация Chainlit entry point.

Читает секцию [chainlit] из BOBA_CONFIG, fallback на env vars.
"""

from __future__ import annotations

from dataclasses import dataclass

from boba_domain import toml_config


@dataclass(frozen=True)
class ChainlitConfig:
    """Настройки Chainlit сервера."""

    root_path: str = ""
    host: str = "0.0.0.0"
    port: int = 8080
    auth_secret: str = ""

    @staticmethod
    def from_env() -> ChainlitConfig:
        toml = toml_config.load_section("chainlit")
        return ChainlitConfig(
            root_path=toml_config.resolve("CHAINLIT_ROOT_PATH", toml, "root_path", ""),
            host=toml_config.resolve("CHAINLIT_HOST", toml, "host", "0.0.0.0"),
            port=int(toml_config.resolve("CHAINLIT_PORT", toml, "port", "8080")),
            auth_secret=toml_config.resolve(
                "CHAINLIT_AUTH_SECRET", toml, "auth_secret"
            ),
        )
