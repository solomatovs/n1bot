"""IndexerExtensionContext: окружение для register_readers / register_sources."""

from __future__ import annotations

from dataclasses import dataclass

from boba.config.app import AppConfig

__all__ = ["IndexerExtensionContext"]


@dataclass(frozen=True)
class IndexerExtensionContext:
    """Контекст, который entry-point плагин получает при регистрации.

    Содержит готовый `AppConfig` — плагин может прочитать свою
    `ConfigSection`, чтобы решить, что регистрировать (по аналогии с
    `boba.tools.ExtensionContext`).
    """

    config: AppConfig
