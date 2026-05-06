"""httpx-клиент для runtime-tools Confluence (search, page outline/section).

Раньше жил в `boba-confluence-shared` как `ConfluenceConnection.make_discovery_client`.
По правилу domain-rule httpx убран из домена; фабрика теперь — в ext-пакете.
Дублируется в `boba-ext-confluence-indexing.http_client` (~30 строк, граница чище
чем общий микропакет).
"""

from __future__ import annotations

from typing import Any

import httpx

from boba.confluence import ConfluenceConnection, ConfluenceConnectionConfig

__all__ = ["ConfluenceHttpClient"]


class ConfluenceHttpClient:
    """Фабрика httpx.Client для Confluence REST (base_url + auth + timeout)."""

    @staticmethod
    def make(cfg: ConfluenceConnectionConfig) -> httpx.Client:
        kwargs: dict[str, Any] = {
            "base_url": cfg.base_url.rstrip("/"),
            "timeout": cfg.timeout_sec,
        }
        ConfluenceConnection.make_auth(cfg)(kwargs)
        return httpx.Client(**kwargs)
