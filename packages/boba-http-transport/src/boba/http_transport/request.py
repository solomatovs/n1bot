"""HttpRequest: DTO для HttpTransport."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from boba.indexing import AuthApplier

__all__ = ["HttpRequest"]


@dataclass(frozen=True)
class HttpRequest:
    """План одного HTTP-запроса.

    `auth` — callable, который мутирует kwargs httpx.Client (добавляет
    Authorization-header или auth-tuple). Transport не знает про PAT/Basic/
    OAuth — просто вызывает callback.

    `source_id` ставится RequestSource'ом — canonical id итогового документа
    (например `confluence://host/page/12345`). Если RequestSource не знает —
    можно использовать `url` как fallback.

    `metadata` — обогащение для Section.metadata: page_id, space_key и т.п.
    """

    url: str
    method: str = "GET"
    headers: Mapping[str, str] = field(default_factory=dict)
    auth: AuthApplier | None = None
    source_id: str = ""
    metadata: Mapping[str, str] = field(default_factory=dict)
