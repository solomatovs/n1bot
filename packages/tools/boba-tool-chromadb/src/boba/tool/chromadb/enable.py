"""Predicate factory для `@tool(enable_if=...)` в chromadb-плагине."""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated

from boba.tool.chromadb.config import ChromadbPluginConfig
from boba.tools import FromConfig

__all__ = ["chromadb_active", "chromadb_enable_if"]


def chromadb_active(
    cfg: Annotated[ChromadbPluginConfig, FromConfig()],
) -> bool:
    """Provider включён при `enable=True`. persist_path валидируется конфигом."""
    return cfg.enable


def chromadb_enable_if(tool_name: str) -> Callable[..., bool]:
    """Predicate: tool регистрируется при включённом плагине + allowlist."""

    def _predicate(
        cfg: Annotated[ChromadbPluginConfig, FromConfig()],
    ) -> bool:
        if not cfg.enable:
            return False
        if cfg.tools is None:
            return True
        return tool_name in cfg.tools

    _predicate.__name__ = f"_chromadb_{tool_name}_enabled"
    return _predicate
