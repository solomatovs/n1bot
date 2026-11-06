"""Общие способы входа: проверка credentials без токенов и транспорта."""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict

from boba.identity.authentication import AuthenticatedSession
from boba.identity.errors import AuthenticationError, ExternalServiceError
from boba.identity.signin import PasswordSignIn, ProxyRequest, ProxySignIn, SignInResult
from boba.identity.sso import (
    SessionRefresh,
    SpnegoExchange,
    SsoChallenge,
    SsoRefused,
    SsoRequest,
    SsoSigned,
)


class SignInProviders(BaseModel):
    """Какие способы входа настроены."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    password: bool
    sso: bool
    proxy: bool


class SignIns:
    """Способы входа процесса: пароль, SPNEGO и доверенный заголовок.

    Ядро входа, общее для всех приложений: по предъявленному отдаёт итог
    входа SignInResult (кто вошёл, роли, профили) и ничего не знает о токене,
    cookie и хранении пользователей. Собирается провайдером из секций [auth]
    (AuthBackends); поверх него стоят слой сессии приложения с браузером
    (BrowserSessions) и провайдер авторизации fastmcp сервиса boba-mcp.
    """

    def __init__(
        self,
        password: PasswordSignIn | None,
        sso: SpnegoExchange | None,
        proxy: ProxySignIn | None,
        renewals: Mapping[str, SessionRefresh] | None = None,
    ) -> None:
        self._password = password
        self._sso = sso
        self._proxy = proxy
        self._renewals: dict[str, SessionRefresh] = {}
        if renewals is not None:
            self._renewals.update(renewals)

    def providers(self) -> SignInProviders:
        return SignInProviders(
            password=self._password is not None,
            sso=bool(self._sso),
            proxy=bool(self._proxy),
        )

    def has_sso(self) -> bool:
        return bool(self._sso)

    async def by_password(self, username: str, password: str) -> SignInResult:
        if self._password is None:
            message = (
                f"password sign-in of {username!r}: [auth] has no local or ldap "
                "provider configured"
            )
            raise ExternalServiceError("auth", message)

        signed = await self._password.sign_in(username, password)
        if signed is None:
            message = (
                f"Invalid username or password: no [auth] provider accepted "
                f"the sign-in of {username!r}"
            )
            raise AuthenticationError(message)

        return signed

    async def by_proxy(self, request: ProxyRequest) -> SignInResult:
        """Вход по подписанному заголовку доверенного бэкенда."""
        if not self._proxy:
            message = (
                f"proxy sign-in of {request.login!r} from {request.client}: "
                "[auth] has no proxy provider configured"
            )
            raise ExternalServiceError("auth", message)

        return await self._proxy.sign_in(request)

    async def admit_proxy(self, request: ProxyRequest) -> SignInResult:
        """Допуск по утверждению доверенного бэкенда, которое вызывающий уже
        проверил (обмен утверждения на токен у сервиса boba-mcp)."""
        if not self._proxy:
            message = (
                f"proxy admission of {request.login!r} from {request.client}: "
                "[auth] has no proxy provider configured"
            )
            raise ExternalServiceError("auth", message)

        return await self._proxy.admit(request)

    def exchange(self) -> SpnegoExchange:
        """SPNEGO-обмен процесса; без kerberos в [auth] — ExternalServiceError."""
        if not self._sso:
            message = (
                "SPNEGO exchange requested but [auth] has no kerberos provider "
                "configured"
            )
            raise ExternalServiceError("auth", message)

        return self._sso

    async def renew(
        self, request: SsoRequest, session: AuthenticatedSession
    ) -> SsoChallenge | SsoRefused | SsoSigned:
        identity = session.principal.identity
        key = session.renewal.value
        refresh = self._renewals.get(key)
        if refresh is None:
            return SsoRefused(
                reason=f"Session refresh {key!r} is unavailable; sign in again."
            )

        result = await refresh.renew(request, session)
        if isinstance(result, SsoSigned):
            if result.signed.identity != identity:
                return SsoRefused(
                    reason="Session refresh resolved to another user; sign in again."
                )

        return result
