"""Вход пользователя: пароль, SPNEGO, выпуск и чтение токена — одна точка для обоих
приложений. Строка users заводится входом; чужой токен строку не заводит.

Ошибки:
AuthenticationError — логин или пароль неверен, токен не принят либо у входа нет
    строки users.
AuthorizationError — вход запрещён провайдером: роли, исключения.
ExternalServiceError — каталог входа недоступен или способ входа не настроен.
InternalServiceError — ошибка конфига каталога или kerberos на нашей стороне.
"""

from __future__ import annotations

import logging
import time
from typing import Protocol

from pydantic import BaseModel, ConfigDict

from boba.auth.tokens import JwtTokens
from boba.identity.api import (
    AuthenticatedUser,
    Authenticator,
    PersistedUsers,
    UsersUpsert,
)
from boba.identity.context import DelegatedTicket
from boba.identity.errors import AuthenticationError, ExternalServiceError
from boba.identity.signin import PasswordSignIn, SignedIn
from boba.identity.sso import (
    SpnegoExchange,
    SsoChallenge,
    SsoRefused,
    SsoRequest,
    SsoSigned,
)
from boba.identity.token import (
    CookieJar,
    CookieSpec,
    RenewVerdict,
    SessionClaims,
    SessionRenewal,
    TokenRejectedError,
)

__all__ = ["AuthService", "AuthUsers", "IssuedSession", "SignInProviders"]

logger = logging.getLogger(__name__)


class AuthUsers(PersistedUsers, UsersUpsert, Protocol):
    """Строки users приложения: найти по идентификатору либо завести входом."""


class SignInProviders(BaseModel):
    """Какие способы входа настроены."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    password: bool
    sso: bool


class IssuedSession(BaseModel):
    """Итог входа: кто вошёл, его строка users и токен сессии."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    signed: SignedIn
    user: AuthenticatedUser
    token: str


class AuthService(Authenticator):
    """Вход по паролю и SPNEGO, токен и cookie сессии, пользователь по токену."""

    def __init__(  # noqa: PLR0913 — сервис входа собирается всеми зависимостями сразу
        self,
        tokens: JwtTokens,
        cookie: CookieSpec,
        password: PasswordSignIn | None,
        sso: SpnegoExchange | None,
        users: AuthUsers,
        renewal: SessionRenewal,
    ) -> None:
        self._tokens = tokens
        self._cookie = cookie
        self._password = password
        self._sso = sso
        self._users = users
        self._renewal = renewal

    @property
    def renewal(self) -> SessionRenewal:
        return self._renewal

    def providers(self) -> SignInProviders:
        return SignInProviders(
            password=self._password is not None, sso=self._sso is not None
        )

    def cookie(self) -> CookieSpec:
        return self._cookie

    def jar(self) -> CookieJar:
        return CookieJar(self._cookie.name)

    async def by_password(self, username: str, password: str) -> IssuedSession:
        signed = await self.sign_in(username, password)

        return await self.issue(signed)

    async def sign_in(self, username: str, password: str) -> SignedIn:
        """Итог входа по паролю без строки users и токена: для хоста, который
        заводит пользователя и выпускает сессию сам (chainlit).
        """
        if self._password is None:
            raise ExternalServiceError("auth", "password sign-in is not configured")

        signed = await self._password.sign_in(username, password)
        if signed is None:
            raise AuthenticationError("Invalid username or password")

        return signed

    async def by_spnego(self, request: SsoRequest) -> SsoChallenge | IssuedSession:
        outcome = await self._exchange().handshake(request)
        if isinstance(outcome, SsoChallenge):
            return outcome

        return await self.issue(outcome.signed)

    async def refresh(
        self, request: SsoRequest, token: str | None
    ) -> SsoChallenge | SsoRefused | IssuedSession:
        session = None
        if token is not None:
            session = self.ticket_of_token(token)

        outcome = await self._exchange().refresh(request, session)
        if isinstance(outcome, SsoSigned):
            return await self.issue(outcome.signed)

        return outcome

    async def refresh_session(
        self, request: SsoRequest, token: str | None
    ) -> SsoChallenge | SsoRefused | IssuedSession:
        """Обновление живой сессии по её виду входа: SPNEGO-обмен для kerberos, иначе
        перевыпуск JWT; отказ — SsoRefused с причиной, приложение разлогинивает.
        """
        if token is None:
            return SsoRefused(reason="request carries no sign-in token")

        try:
            claims = self.stale_claims(token)
        except AuthenticationError as exc:
            return SsoRefused(reason=str(exc))

        if claims.sign_in().is_kerberos():
            return await self._refresh_kerberos(request, token)

        return await self._renew_session(request, token)

    async def _refresh_kerberos(
        self, request: SsoRequest, token: str
    ) -> SsoChallenge | SsoRefused | IssuedSession:
        if self._sso is None:
            return SsoRefused(reason="kerberos sign-in without sso configured")

        return await self.refresh(request, token)

    async def _renew_session(
        self, request: SsoRequest, token: str
    ) -> SsoRefused | IssuedSession:
        """Перевыпуск JWT только по своему запросу страницы."""
        if not request.own_request:
            reason = f"refresh without its own header [{request.client}]"
            return SsoRefused(reason=reason)

        try:
            return await self.renew(token)
        except AuthenticationError as exc:
            return SsoRefused(reason=str(exc))

    async def issue(self, signed: SignedIn) -> IssuedSession:
        """Строка users по итогу входа и токен сессии с claims этого входа."""
        user = await self._users.ensure_user(signed)
        token = self._tokens.issue(signed)

        return IssuedSession(signed=signed, user=user, token=token)

    async def user_of_token(self, token: str) -> AuthenticatedUser:
        """Пользователь входа: строка users по токену, metadata — из токена."""
        try:
            claims = self._tokens.read(token)
        except TokenRejectedError as exc:
            message = f"sign-in token rejected: {exc.reason}"
            raise AuthenticationError(message) from exc

        stored = await self._users.get_user(claims.identifier)
        if stored is None:
            raise AuthenticationError(f"sign-in of {claims.identifier!r} not persisted")

        return AuthenticatedUser(
            id=stored.id,
            identifier=stored.identifier,
            sign_in=claims.sign_in(),
            settings=stored.settings,
        )

    def ticket_of_token(self, token: str) -> DelegatedTicket | None:
        """Билет SSO-входа из токена; None — токен негоден или вход не через SPNEGO.

        Токен принимается и чуть просроченным (grace): вкладка, проспавшая сигнал,
        ещё может обменяться.
        """
        try:
            claims = self._tokens.read_stale(token, self._renewal.grace_sec)
        except TokenRejectedError as exc:
            logger.info("sign-in token rejected on refresh: %s", exc.reason)
            return None

        return claims.ticket()

    def stale_claims(self, token: str) -> SessionClaims:
        """Claims токена в пределах grace; AuthenticationError — токен не принят."""
        try:
            return self._tokens.read_stale(token, self._renewal.grace_sec)
        except TokenRejectedError as exc:
            message = f"sign-in token rejected: {exc.reason}"
            raise AuthenticationError(message) from exc

    async def renew(self, token: str) -> IssuedSession:
        """Перевыпуск JWT без нового входа: тот же вход, новый срок, потолок сессии.

        Kerberos-вход так не продлевается — ему нужен SPNEGO-обмен, иначе билет
        для инструментов и роли остались бы прежними.
        """
        claims = self.stale_claims(token)
        signed = claims.signed()
        if signed.sign_in.is_kerberos():
            msg = "a kerberos sign-in renews only by a SPNEGO refresh"
            raise AuthenticationError(msg)

        verdict = self._renewal.verdict(claims, int(time.time()))
        if verdict is not RenewVerdict.RENEWABLE:
            msg = f"sign-in session cannot be renewed: {verdict}"
            raise AuthenticationError(msg)

        stored = await self._users.get_user(claims.identifier)
        if stored is None:
            raise AuthenticationError(f"sign-in of {claims.identifier!r} not persisted")

        user = AuthenticatedUser(
            id=stored.id,
            identifier=stored.identifier,
            sign_in=signed.sign_in,
            settings=stored.settings,
        )

        return IssuedSession(signed=signed, user=user, token=self._tokens.renew(claims))

    def _exchange(self) -> SpnegoExchange:
        if self._sso is None:
            raise ExternalServiceError("auth", "sso is not configured")

        return self._sso
