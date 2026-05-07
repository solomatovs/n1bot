"""HttpRequest: DTO для HttpTransport."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from boba.processing import AuthApplier

__all__ = ["HttpRequest"]


@dataclass(frozen=True)
class HttpRequest:
    """План одного HTTP-запроса.

    `auth` — callable, который мутирует kwargs httpx.Client (добавляет
    Authorization-header или auth-tuple). Transport не знает про PAT/Basic/
    OAuth — просто вызывает callback.

    `source_id` ставится RequestSource'ом — стабильный URL по которому
    документ доступен (для Confluence — viewpage URL, отдельный от REST URL
    запроса). HttpTransport не fallback'ит — пустой source_id это ошибка
    сборки RequestSource'а.

    `metadata` — обогащение для Section.metadata: page_id, space_key и т.п.
    """

    url: str
    method: str = "GET"
    headers: Mapping[str, str] = field(default_factory=dict)
    auth: AuthApplier | None = None
    source_id: str = ""
    metadata: Mapping[str, str] = field(default_factory=dict)
