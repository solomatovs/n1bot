"""HTTP-auth callables: PatAuth, BasicAuth.

Реализуют `AuthApplier = Callable[[dict[str, Any]], None]` из boba-indexing.
RequestSource создаёт нужный callable из своих credentials и кладёт в
`HttpRequest.auth`. HttpTransport вызывает callable, не зная про PAT/Basic.
"""

from __future__ import annotations

from typing import Any

__all__ = ["BasicAuth", "PatAuth"]


class PatAuth:
    """Atlassian Personal Access Token: Authorization: Bearer <token>."""

    def __init__(self, token: str) -> None:
        self._token = token

    def __call__(self, kwargs: dict[str, Any]) -> None:
        headers = dict(kwargs.get("headers") or {})
        headers["Authorization"] = f"Bearer {self._token}"
        kwargs["headers"] = headers


class BasicAuth:
    """HTTP basic-auth: httpx.Client(auth=(user, password))."""

    def __init__(self, user: str, password: str) -> None:
        self._user = user
        self._password = password

    def __call__(self, kwargs: dict[str, Any]) -> None:
        kwargs["auth"] = (self._user, self._password)
