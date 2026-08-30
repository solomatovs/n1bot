"""SSO-вход: запрос обмена без транспорта, исходы, коды отказа и порты.

Ошибки: своих не выпускает; AuthorizationError — отказ в допуске у реализации.
"""

from __future__ import annotations

import base64
import html
from abc import abstractmethod
from collections.abc import Sequence
from enum import StrEnum
from typing import ClassVar, Protocol

from pydantic import BaseModel, ConfigDict

from boba.identity.context import DelegatedTicket
from boba.identity.signin import SignedIn

__all__ = [
    "NegotiateToken",
    "RefreshSignal",
    "RequestHeader",
    "SpnegoExchange",
    "SsoAdmission",
    "SsoChallenge",
    "SsoErrorCode",
    "SsoRefresh",
    "SsoRefused",
    "SsoRequest",
    "SsoSigned",
]


class RequestHeader(StrEnum):
    """Заголовки, которыми живёт обмен: токен, клиент за прокси, вызов браузеру."""

    AUTHORIZATION = "authorization"
    FORWARDED_FOR = "x-forwarded-for"
    REAL_IP = "x-real-ip"
    WWW_AUTHENTICATE = "WWW-Authenticate"


class SsoRefresh(StrEnum):
    """Признак того, что обмен запросила своя страница, а не чужой сайт."""

    HEADER = "x-boba-sso-refresh"
    VALUE = "1"

    @classmethod
    def asked(cls, value: str) -> bool:
        """Заголовок ставит только свой fetch: кросс-сайтовый запрос его не несёт."""
        return value == cls.VALUE


class SsoErrorCode(StrEnum):
    """Коды исхода SSO для страницы логина: ?error=<code>."""

    TICKET = "sso_ticket"
    DENIED = "sso_denied"
    FAILED = "sso_failed"

    def login_url(self, login: str) -> str:
        return f"{login}?error={self.value}"

    def challenge_page(self, login: str) -> str:
        """Тело 401: с тикетом браузер повторит запрос сам, без него уйдёт на логин."""
        url = html.escape(self.login_url(login), quote=True)

        return f'<!doctype html><meta http-equiv="refresh" content="0;url={url}">'


class SsoRequest(BaseModel):
    """Запрос SPNEGO-обмена глазами сервиса: заголовок Authorization, признак своего
    fetch и адрес клиента для журнала. Собирается адаптером транспорта.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    UNKNOWN_CLIENT: ClassVar[str] = "unknown"

    authorization: str = ""
    refresh_asked: bool = False
    client: str = UNKNOWN_CLIENT


class NegotiateToken:
    """Токен Negotiate из заголовка Authorization."""

    SCHEME: ClassVar[str] = "negotiate"

    @classmethod
    def of(cls, authorization: str) -> bytes | str:
        """Токен либо причина, почему его нет."""
        if not authorization:
            return "no Authorization header"

        scheme, _, value = authorization.partition(" ")
        if scheme.lower() != cls.SCHEME:
            return f"unexpected auth scheme {scheme!r}"

        if not value:
            return f"unexpected auth scheme {scheme!r}"

        try:
            return base64.b64decode(value)
        except ValueError as e:
            return f"invalid base64 token: {e}"


class SsoChallenge(BaseModel):
    """Личности нет: ответить 401 Negotiate, браузер домена повторит с токеном."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    HEADERS: ClassVar[dict[str, str]] = {
        RequestHeader.WWW_AUTHENTICATE.value: "Negotiate"
    }

    reason: str
    level: int


class SsoSigned(BaseModel):
    """SPNEGO принят, принципал допущен: пользователь входа с билетом в metadata."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    signed: SignedIn
    principal: str


class SsoRefused(BaseModel):
    """Повторный обмен не относится к этой сессии: 403, повтор не поможет."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    reason: str


class SsoAdmission(Protocol):
    """Допуск принципала ко входу: роли этого входа либо отказ."""

    @abstractmethod
    async def roles_of(self, principal: str, group_sids: Sequence[str]) -> list[str]:
        """Роли принципала; AuthorizationError — вход запрещён."""


class SpnegoExchange(Protocol):
    """SPNEGO-обмен: вход и молчаливое обновление билета живой сессии."""

    @abstractmethod
    async def handshake(self, request: SsoRequest) -> SsoChallenge | SsoSigned: ...

    @abstractmethod
    async def refresh(
        self, request: SsoRequest, session: DelegatedTicket | None
    ) -> SsoChallenge | SsoRefused | SsoSigned: ...


class RefreshSignal(Protocol):
    """Просьба к фронту молча пройти SPNEGO ещё раз; реализация — у приложения."""

    @abstractmethod
    async def send(self) -> bool:
        """True — сигнал ушёл живому слушателю; False — слушать некому."""
