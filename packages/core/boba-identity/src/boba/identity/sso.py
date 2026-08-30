"""SSO-вход: запрос обмена без транспорта, исходы, коды отказа и порты.

Ошибки: своих не выпускает; AuthorizationError — отказ в допуске у реализации.
"""

from __future__ import annotations

import html
from abc import abstractmethod
from enum import StrEnum
from typing import ClassVar, Protocol

from pydantic import BaseModel, ConfigDict

from boba.identity.admission import PrincipalFacts
from boba.identity.context import DelegatedTicket
from boba.identity.signin import SignedIn

__all__ = [
    "OwnRequest",
    "RefreshSignal",
    "RequestHeader",
    "SpnegoExchange",
    "SsoAdmission",
    "SsoChallenge",
    "SsoErrorCode",
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


class OwnRequest(StrEnum):
    """Метка запроса своей страницы: заголовок ставит только свой fetch, кросс-сайтовая
    форма или навигация его не несут — им закрыты вход, выход и повторный обмен.
    """

    HEADER = "x-boba-request"
    VALUE = "1"

    @classmethod
    def asked(cls, value: str) -> bool:
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
    """Запрос входа глазами сервиса: заголовок Authorization, метка своего fetch и
    адрес клиента для журнала. Собирается адаптером транспорта.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    UNKNOWN_CLIENT: ClassVar[str] = "unknown"

    authorization: str = ""
    own_request: bool = False
    client: str = UNKNOWN_CLIENT


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
    async def roles_of(self, facts: PrincipalFacts) -> list[str]:
        """Роли по фактам SPNEGO-входа; AuthorizationError — вход запрещён."""


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
