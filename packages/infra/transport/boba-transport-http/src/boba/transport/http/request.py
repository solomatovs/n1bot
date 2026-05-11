"""HttpRequest — DTO для HttpTransport."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

import httpx

from boba.indexing import Metadata, SourceId

__all__ = ["HttpRequest"]


@dataclass(frozen=True)
class HttpRequest:
    """План одного HTTP-запроса.

    `auth` — `httpx.Auth`-наследник (или `None`). Transport передаёт его
    напрямую в `httpx.Client(auth=...)`; Transport не знает про PAT/Basic/
    OAuth.

    `source_id` — caller-supplied canonical id (RequestSource ставит,
    Transport исполняет — Transport не формирует identity). Для Confluence
    обычно viewpage URL, отдельный от REST URL запроса.

    `metadata` — обогащение для Section.metadata: page_id, space_key и т.п.
    """

    url: str
    source_id: SourceId
    method: str = "GET"
    headers: Mapping[str, str] = field(default_factory=dict)
    auth: httpx.Auth | None = None
    metadata: Metadata = field(default_factory=Metadata.empty)
