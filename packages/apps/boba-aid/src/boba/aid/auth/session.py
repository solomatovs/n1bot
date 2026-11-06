"""Браузерная сессия AID: выпуск JWT, cookie и выбор способа продления."""

from __future__ import annotations

import logging
import time

from pydantic import BaseModel, ConfigDict, Field

from boba.aid.auth.claims import SessionClaims, TokenRejectedError
from boba.aid.auth.cookies import CookieJar, CookieSpec
from boba.aid.auth.policy import RenewVerdict, SessionRenewal
from boba.aid.auth.tokens import JwtTokens
from boba.auth import SessionService, SignInProviders, SignIns
from boba.identity.api import Authenticator, StoredUser
from boba.identity.authentication import (
    AuthenticatedPrincipal,
    AuthenticatedSession,
    RenewalKind,
)
from boba.identity.context import DelegatedTicket
from boba.identity.errors import AuthenticationError
from boba.identity.signin import ProxyRequest, SignInResult
from boba.identity.sso import (
    SpnegoExchange,
    SsoChallenge,
    SsoRefused,
    SsoRequest,
    SsoSigned,
)

logger = logging.getLogger(__name__)


class IssuedSession(BaseModel):
    """Подтверждённая сессия: данные входа, пользователь и выданный ему токен."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    session: AuthenticatedSession
    token: str = Field(repr=False)
    preferred_profile: str = ""

    @property
    def user(self) -> AuthenticatedPrincipal:
        return self.session.principal


class BrowserSessions(Authenticator):
    """Сессия браузера поверх способов входа: выпуск и проверка токена,
    cookie и явное продление с текущими данными пользователя.

    Создаётся провайдером процесса чата из SignIns, читателя токенов и
    секции [session]. Выпуск и продление читают users; проверка подписанного
    токена локальная.
    """

    def __init__(
        self,
        sign_ins: SignIns,
        tokens: JwtTokens,
        cookie: CookieSpec,
        sessions: SessionService,
        renewal: SessionRenewal,
    ) -> None:
        self._sign_ins = sign_ins
        self._tokens = tokens
        self._cookie = cookie
        self._sessions = sessions
        self._renewal = renewal

    @property
    def renewal(self) -> SessionRenewal:
        return self._renewal

    def providers(self) -> SignInProviders:
        return self._sign_ins.providers()

    def cookie(self) -> CookieSpec:
        return self._cookie

    @property
    def tokens(self) -> JwtTokens:
        return self._tokens

    def jar(self) -> CookieJar:
        return CookieJar(self._cookie.name)

    async def by_password(self, username: str, password: str) -> IssuedSession:
        signed = await self._sign_ins.by_password(username, password)

        return await self.issue(signed)

    async def by_proxy(self, request: ProxyRequest) -> IssuedSession:
        """Вход по подписанному заголовку доверенного бэкенда."""
        signed = await self._sign_ins.by_proxy(request)

        return await self.issue(signed)

    async def by_spnego(self, request: SsoRequest) -> SsoChallenge | IssuedSession:
        outcome = await self._exchange().handshake(request)
        if isinstance(outcome, SsoChallenge):
            return outcome

        return await self.issue(outcome.signed)

    async def refresh_session(
        self, request: SsoRequest, token: str | None
    ) -> SsoChallenge | SsoRefused | IssuedSession:
        """Обновляет сессию через её провайдера; отказ требует нового входа."""
        try:
            if token is None:
                raise AuthenticationError("session refresh requires a sign-in token")
            claims = self.stale_claims(token)
            now = int(time.time())
            if self._renewal.verdict(claims, now) is not RenewVerdict.RENEWABLE:
                raise AuthenticationError(
                    "the session lifetime is exhausted; sign in again"
                )
            if claims.renewal is RenewalKind.REAUTHENTICATE:
                raise AuthenticationError("this session requires a new sign-in")
            if claims.renewal is RenewalKind.JWT:
                return await self._renew_session(request, token)

            await self.stored_user(claims.principal())
            outcome = await self._sign_ins.renew(request, claims.session())
            if isinstance(outcome, SsoSigned):
                return await self.issue(outcome.signed, previous=claims)
            return outcome
        except (AuthenticationError, TokenRejectedError) as exc:
            return SsoRefused(reason=str(exc))

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

    async def issue(
        self, signed: SignInResult, *, previous: SessionClaims | None = None
    ) -> IssuedSession:
        """Сохраняет назначения провайдера и выпускает сессию с итоговыми правами."""
        session = await self._sessions.accept(
            signed,
            previous=previous.principal() if previous is not None else None,
        )
        token = self._tokens.issue(
            session,
            int(time.time()),
            auth_time=previous.auth_time if previous is not None else None,
        )
        preferred = (
            signed.preferred_profile
            if signed.preferred_profile in session.principal.profiles
            else ""
        )
        return IssuedSession(session=session, token=token, preferred_profile=preferred)

    async def user_of_token(self, token: str) -> AuthenticatedPrincipal:
        return (await self.session_of_token(token)).user

    async def session_of_token(self, token: str) -> IssuedSession:
        """Подтверждает JWT и восстанавливает подписанный снимок прав без чтения БД."""
        try:
            claims = self._tokens.read(token)
        except TokenRejectedError as exc:
            raise AuthenticationError(
                f"sign-in token rejected as {exc.reason}"
            ) from exc
        return IssuedSession(session=claims.session(), token=token)

    async def stored_user(self, principal: AuthenticatedPrincipal) -> StoredUser:
        """Данные пользователя для UI; проекция не является источником авторизации."""
        return await self._sessions.stored(principal)

    def ticket_of_token(self, token: str) -> DelegatedTicket | None:
        """Билет SSO-входа из токена; None — токен негоден или вход не через SPNEGO.

        Токен принимается и чуть просроченным (grace): вкладка, проспавшая сигнал,
        ещё может обменяться.
        """
        try:
            claims = self._tokens.read_stale(token, self._renewal.grace_sec)
        except TokenRejectedError:
            # в лог идёт причина отказа, сам токен туда не попадает
            logger.info("sign-in token rejected on refresh")
            return None

        return claims.ticket()

    def stale_claims(self, token: str) -> SessionClaims:
        """Claims токена в пределах grace; AuthenticationError — токен не принят."""
        try:
            return self._tokens.read_stale(token, self._renewal.grace_sec)
        except TokenRejectedError as exc:
            message = f"sign-in token rejected as {exc.reason}: {exc}"
            raise AuthenticationError(message) from exc

    async def renew(self, token: str) -> IssuedSession:
        """Обновляет снимок прав; сохраняет исходное время входа и credentials."""
        claims = self.stale_claims(token)
        if claims.renewal is not RenewalKind.JWT:
            raise AuthenticationError(
                "this session requires its authentication provider to refresh"
            )
        now = int(time.time())
        if self._renewal.verdict(claims, now) is not RenewVerdict.RENEWABLE:
            raise AuthenticationError(
                "the session lifetime is exhausted; sign in again"
            )
        session = claims.session()
        principal = await self._sessions.current(session.principal)
        session = session.model_copy(update={"principal": principal})
        token = self._tokens.issue(session, now, auth_time=claims.auth_time)
        return IssuedSession(session=session, token=token)

    def _exchange(self) -> SpnegoExchange:
        return self._sign_ins.exchange()
