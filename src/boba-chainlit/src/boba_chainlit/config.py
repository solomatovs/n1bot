"""Конфигурация Chainlit entry point.

Читает секцию [chainlit] из BOBA_CONFIG, fallback на env vars.
"""

from __future__ import annotations

from dataclasses import dataclass

from boba_domain.config import _load_toml_section, _resolve


@dataclass(frozen=True)
class ChainlitConfig:
    """Настройки Chainlit сервера."""

    root_path: str = ""
    host: str = "0.0.0.0"
    port: int = 8080

    @staticmethod
    def from_env() -> ChainlitConfig:
        toml = _load_toml_section("chainlit")
        return ChainlitConfig(
            root_path=_resolve("CHAINLIT_ROOT_PATH", toml, "root_path", ""),
            host=_resolve("CHAINLIT_HOST", toml, "host", "0.0.0.0"),
            port=int(_resolve("CHAINLIT_PORT", toml, "port", "8080")),
        )
