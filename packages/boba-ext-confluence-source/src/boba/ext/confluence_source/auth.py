"""Auth для Confluence: PAT (Bearer) или Basic (user+token)."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

import httpx

__all__ = ["AuthError", "BasicAuth", "ConfluenceAuth", "PatAuth"]


class AuthError(Exception):
    """Конфигурация auth неполная или противоречивая."""


class ConfluenceAuth(ABC):
    """Стратегия аутентификации в Confluence REST."""

    @abstractmethod
    def apply(self, client_kwargs: dict) -> None:
        """Дописать auth-параметры в kwargs для httpx.Client(...)."""
        ...


@dataclass(frozen=True)
class PatAuth(ConfluenceAuth):
    """Personal Access Token (Bearer). Modern Atlassian auth."""

    token: str

    def apply(self, client_kwargs: dict) -> None:
        headers = client_kwargs.setdefault("headers", {})
        headers["Authorization"] = f"Bearer {self.token}"


@dataclass(frozen=True)
class BasicAuth(ConfluenceAuth):
    """Basic auth: username + password (или legacy API token)."""

    user: str
    password: str

    def apply(self, client_kwargs: dict) -> None:
        client_kwargs["auth"] = httpx.BasicAuth(self.user, self.password)
