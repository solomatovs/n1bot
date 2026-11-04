"""SSO-вход: запрос обмена без транспорта, исходы и порты.

Ошибки: своих не выпускает; AuthorizationError — отказ в допуске у реализации.
"""

from __future__ import annotations

from abc import abstractmethod
from typing import ClassVar, Protocol

from pydantic import BaseModel, ConfigDict

from boba.identity.admission import PrincipalFacts
from boba.identity.authentication import AuthenticatedSession
from boba.identity.context import DelegatedTicket
from boba.identity.signin import SignInResult

__all__ = [
    "RefreshSignal",
    "SessionRefresh",
    "SpnegoExchange",
    "SsoAdmission",
    "SsoChallenge",
    "SsoRefused",
    "SsoRequest",
    "SsoSigned",
]


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
    """Личности нет: ответить 401 Negotiate, браузер домена повторит с токеном;
    сам HTTP-ответ собирает адаптер приложения.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    reason: str
    level: int


class SsoSigned(BaseModel):
    """SPNEGO принят: типизированный результат входа с credentials."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    signed: SignInResult


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


class SessionRefresh(Protocol):
    """Обновляет credentials подтверждённой сессии способом её провайдера."""

    @abstractmethod
    async def renew(
        self, request: SsoRequest, session: AuthenticatedSession
    ) -> SsoChallenge | SsoRefused | SsoSigned: ...
