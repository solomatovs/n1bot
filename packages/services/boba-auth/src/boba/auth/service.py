"""Вход пользователя слоями: SignIns — способы входа [auth] без токена и
хранения, AuthService — сессия приложения с браузером поверх них (токен,
cookie и продление). Строка users заводится входом; проверка токена
восстанавливает личность и права из подписанных claims без чтения БД.

Ошибки:
AuthenticationError — логин или пароль неверен, подпись proxy-запроса не сходится,
    токен не принят либо у входа нет строки users.
AuthorizationError — вход запрещён провайдером: роли, исключения, адрес клиента.
ExternalServiceError — каталог входа недоступен или способ входа не настроен.
InternalServiceError — ошибка конфига каталога или kerberos на нашей стороне.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Mapping
from typing import Protocol

from pydantic import BaseModel, ConfigDict

from boba.auth.realms import RealmSelection
from boba.auth.tokens import JwtTokens
from boba.identity.api import (
    AuthenticatedUser,
    Authenticator,
    PersistedUsers,
    UsersUpsert,
)
from boba.identity.context import DelegatedTicket
from boba.identity.errors import AuthenticationError, ExternalServiceError
from boba.identity.signin import (
    PasswordSignIn,
    ProfileCatalog,
    ProxyRequest,
    ProxySignIn,
    SessionAccess,
    SignedIn,
    SignInMetadata,
)
from boba.identity.sso import (
    SessionRefresh,
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

__all__ = ["AuthService", "AuthUsers", "IssuedSession", "SignInProviders", "SignIns"]

logger = logging.getLogger(__name__)


class AuthUsers(PersistedUsers, UsersUpsert, Protocol):
    """Строки users приложения: найти по идентификатору либо завести входом."""


class SignInProviders(BaseModel):
    """Какие способы входа настроены."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    password: bool
    sso: bool
    proxy: bool
    password_realms: tuple[str, ...] = ()
    sso_realms: tuple[str, ...] = ()


class IssuedSession(BaseModel):
    """Подтверждённая сессия: данные входа, пользователь и выданный ему токен."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    signed: SignedIn
    user: AuthenticatedUser
    token: str


class SignIns:
    """Способы входа процесса: пароль, SPNEGO и доверенный заголовок.

    Ядро входа, общее для всех приложений: по предъявленному отдаёт итог
    входа SignedIn (кто вошёл, роли, профили) и ничего не знает о токене,
    cookie и хранении пользователей. Собирается провайдером из секций [auth]
    (AuthBackends); поверх него стоят слой сессии приложения с браузером
    (AuthService) и провайдер авторизации fastmcp сервиса boba-mcp.
    """

    def __init__(
        self,
        password: PasswordSignIn | None,
        sso: Mapping[str, SpnegoExchange],
        proxy: Mapping[str, ProxySignIn],
        profiles: ProfileCatalog | None = None,
        renewals: Mapping[tuple[str, str], SessionRefresh] | None = None,
    ) -> None:
        self._password = password
        self._sso = dict(sso)
        self._proxy = dict(proxy)
        self._selection = RealmSelection()
        self._access = SessionAccess(profiles)
        self._renewals: dict[tuple[str, str], SessionRefresh] = {}
        if renewals is not None:
            self._renewals.update(renewals)

    def current(self, sign_in: SignInMetadata, roles: frozenset[str]) -> SignInMetadata:
        return self._access.current(sign_in, roles)

    def resource(
        self, sign_in: SignInMetadata, roles: frozenset[str]
    ) -> SignInMetadata:
        return self._access.resource(sign_in, roles)

    def providers(self) -> SignInProviders:
        realms: tuple[str, ...] = ()
        if self._password is not None:
            realms = tuple(sorted(self._password.realms))

        return SignInProviders(
            password=self._password is not None,
            sso=bool(self._sso),
            proxy=bool(self._proxy),
            password_realms=realms,
            sso_realms=tuple(sorted(self._sso)),
        )

    def has_sso(self) -> bool:
        return bool(self._sso)

    async def by_password(
        self, username: str, password: str, realm: str = ""
    ) -> SignedIn:
        if self._password is None:
            message = (
                f"password sign-in of {username!r}: [auth] has no local or ldap "
                "provider configured"
            )
            raise ExternalServiceError("auth", message)

        signed = await self._password.sign_in(username, password, realm)
        if signed is None:
            message = (
                f"Invalid username or password: no [auth] provider accepted "
                f"the sign-in of {username!r}"
            )
            raise AuthenticationError(message)

        return signed

    async def by_proxy(self, request: ProxyRequest, realm: str = "") -> SignedIn:
        """Вход по подписанному заголовку доверенного бэкенда."""
        if not self._proxy:
            message = (
                f"proxy sign-in of {request.login!r} from {request.client}: "
                "[auth] has no proxy provider configured"
            )
            raise ExternalServiceError("auth", message)

        selected = self._selection.selected(realm, self._proxy, "proxy sign-in")

        return await self._proxy[selected].sign_in(request)

    async def admit_proxy(self, request: ProxyRequest, realm: str = "") -> SignedIn:
        """Допуск по утверждению доверенного бэкенда, которое вызывающий уже
        проверил (обмен утверждения на токен у сервиса boba-mcp)."""
        if not self._proxy:
            message = (
                f"proxy admission of {request.login!r} from {request.client}: "
                "[auth] has no proxy provider configured"
            )
            raise ExternalServiceError("auth", message)

        selected = self._selection.selected(realm, self._proxy, "proxy admission")

        return await self._proxy[selected].admit(request)

    def exchange(self, realm: str = "") -> SpnegoExchange:
        """SPNEGO-обмен процесса; без kerberos в [auth] — ExternalServiceError."""
        if not self._sso:
            message = (
                "SPNEGO exchange requested but [auth] has no kerberos provider "
                "configured"
            )
            raise ExternalServiceError("auth", message)

        selected = self._selection.selected(realm, self._sso, "SPNEGO exchange")

        return self._sso[selected]

    async def renew(
        self, request: SsoRequest, signed: SignedIn
    ) -> SsoChallenge | SsoRefused | SsoSigned:
        identity = signed.sign_in.identity_for(signed.identifier)
        key = (signed.sign_in.renewal, identity.realm)
        refresh = self._renewals.get(key)
        if refresh is None:
            return SsoRefused(
                reason=f"Session refresh {key!r} is unavailable; sign in again."
            )

        result = await refresh.renew(request, signed)
        if isinstance(result, SsoSigned):
            if result.signed.identifier != signed.identifier:
                return SsoRefused(
                    reason="Session refresh resolved to another user; sign in again."
                )

        return result


class AuthService(Authenticator):
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
        users: AuthUsers,
        renewal: SessionRenewal,
    ) -> None:
        self._sign_ins = sign_ins
        self._tokens = tokens
        self._cookie = cookie
        self._users = users
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

    async def by_password(
        self, username: str, password: str, realm: str = ""
    ) -> IssuedSession:
        signed = await self.sign_in(username, password, realm)

        return await self.issue(signed)

    async def sign_in(self, username: str, password: str, realm: str = "") -> SignedIn:
        """Итог входа по паролю без строки users и токена: для хоста, который
        заводит пользователя и выпускает сессию сам (чат aid). Вход уже помечен
        поколением сессий, чтобы токен хоста прошёл проверку.
        """
        signed = await self._sign_ins.by_password(username, password, realm)

        return self._tokens.stamp(signed)

    async def by_proxy(self, request: ProxyRequest, realm: str = "") -> IssuedSession:
        """Вход по подписанному заголовку доверенного бэкенда."""
        signed = await self._sign_ins.by_proxy(request, realm)

        return await self.issue(signed)

    async def by_spnego(
        self, request: SsoRequest, realm: str = ""
    ) -> SsoChallenge | IssuedSession:
        outcome = await self._exchange(realm).handshake(request)
        if isinstance(outcome, SsoChallenge):
            return outcome

        return await self.issue(outcome.signed)

    async def refresh(
        self, request: SsoRequest, token: str | None
    ) -> SsoChallenge | SsoRefused | IssuedSession:
        session = None
        realm = ""
        identifier = ""
        if token is not None:
            try:
                claims = self.stale_claims(token)
            except AuthenticationError as exc:
                return SsoRefused(reason=str(exc))

            session = claims.ticket()
            identifier = claims.identifier
            identity = claims.sign_in().identity
            if identity is not None:
                realm = identity.realm

        outcome = await self._exchange(realm).refresh(request, session)
        if not isinstance(outcome, SsoSigned):
            return outcome

        if outcome.signed.identifier != identifier:
            reason = (
                "SPNEGO refresh resolved to another user identifier; "
                "sign out and sign in again"
            )
            return SsoRefused(reason=reason)

        return await self.issue(outcome.signed)

    async def refresh_session(
        self, request: SsoRequest, token: str | None
    ) -> SsoChallenge | SsoRefused | IssuedSession:
        """Обновление через порт провайдера либо перевыпуск JWT без credentials.
        Отказ — SsoRefused с причиной, приложение разлогинивает.
        """
        if token is None:
            reason = (
                f"session refresh from [{request.client}]: request carries no "
                "sign-in token"
            )
            return SsoRefused(reason=reason)

        try:
            claims = self.stale_claims(token)
        except AuthenticationError as exc:
            return SsoRefused(reason=str(exc))

        if claims.sign_in().renewal:
            outcome = await self._sign_ins.renew(request, claims.signed())
            if isinstance(outcome, SsoSigned):
                return await self.issue(outcome.signed)

            return outcome

        return await self._renew_session(request, token)

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
        """Заводит строку users и выпускает токен с UUID и правами пользователя."""
        stamped = self._tokens.stamp(signed)
        user = await self._users.ensure_user(stamped)
        stamped = stamped.model_copy(
            update={"sign_in": self._sign_ins.current(stamped.sign_in, user.roles)}
        )
        user = user.model_copy(update={"sign_in": stamped.sign_in})
        issued_at = int(time.time())
        token = self._tokens.issue(stamped, user.id, user.created_at, issued_at)

        return IssuedSession(signed=stamped, user=user, token=token)

    async def user_of_token(self, token: str) -> AuthenticatedUser:
        """Личность и роли из проверенного JWT; чтения БД здесь нет."""
        session = await self.session_of_token(token)

        return session.user

    async def session_of_token(self, token: str) -> IssuedSession:
        """Читает действующую сессию из JWT без чтения БД и перевыпуска токена."""
        try:
            claims = self._tokens.read(token)
        except TokenRejectedError as exc:
            message = f"sign-in token rejected as {exc.reason}: {exc}"
            raise AuthenticationError(message) from exc

        signed = claims.signed()
        user = claims.authenticated_user()

        return IssuedSession(signed=signed, user=user, token=token)

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
        """Перевыпуск JWT без нового входа: тот же вход, новый срок, потолок сессии.

        Kerberos-вход так не продлевается — ему нужен SPNEGO-обмен, иначе билет
        для инструментов и роли остались бы прежними.
        """
        claims = self.stale_claims(token)
        signed = claims.signed()
        if signed.sign_in.renewal:
            msg = (
                f"jwt renewal of {claims.identifier!r}: the session requires "
                f"refresh method {signed.sign_in.renewal!r}"
            )
            raise AuthenticationError(msg)

        now = int(time.time())
        verdict = self._renewal.verdict(claims, now)
        if verdict is not RenewVerdict.RENEWABLE:
            msg = (
                f"jwt renewal of {claims.identifier!r} at {now}: session cannot "
                f"be renewed, verdict {verdict} (exp {claims.exp}, iat {claims.iat})"
            )
            raise AuthenticationError(msg)

        user = await self._current_user(claims)
        signed = signed.model_copy(update={"sign_in": user.sign_in})
        claims = claims.model_copy(
            update={
                "metadata": user.sign_in.render(),
                "created_at": user.created_at,
            }
        )

        renewed = self._tokens.renew(claims, now)

        return IssuedSession(signed=signed, user=user, token=renewed)

    async def _current_user(self, claims: SessionClaims) -> AuthenticatedUser:
        """Читает пользователя только при явном продлении сессии."""
        stored = await self._users.get_user(claims.identifier)
        if stored is None:
            message = (
                f"sign-in of {claims.identifier!r} not persisted: the token is "
                "valid but the users table has no row with this identifier"
            )
            raise AuthenticationError(message)

        if stored.id != claims.user_id:
            message = (
                f"sign-in token of {claims.identifier!r} names user UUID "
                f"{claims.user_id}, but users stores {stored.id}: sign in again"
            )
            raise AuthenticationError(message)

        return AuthenticatedUser(
            id=stored.id,
            identifier=stored.identifier,
            created_at=stored.created_at,
            sign_in=self._sign_ins.current(claims.sign_in(), stored.roles),
        )

    def _exchange(self, realm: str = "") -> SpnegoExchange:
        return self._sign_ins.exchange(realm)
