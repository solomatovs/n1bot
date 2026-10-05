"""Вход по утверждению доверенного бэкенда: он называет логин пользователя и
подтверждает запрос общим секретом, роли — провайдерами roles.* конфига.
Упаковок две: подпись HMAC по заголовкам (ProxySignature, чат и studio) и
подписанный JWT для обмена на токен (ProxyAssertions, сервис boba-mcp); допуск
после проверки — общий (HmacProxySignIn.admit).

Ошибки:
AuthenticationError — подпись не сходится, метка времени вне окна, заголовки
    пусты.
AssertionRejectedError — утверждение JWT не принято: подпись, срок, издатель,
    получатель либо повтор.
AuthorizationError — адрес клиента вне allowed_clients, исключение по логину
    или ни одной роли при require_roles.
ExternalServiceError — каталог ролей недоступен.
InternalServiceError — служебный bind или поиск в каталоге отвергнуты.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import time
from enum import StrEnum
from ipaddress import ip_address
from typing import ClassVar
from uuid import uuid4

import jwt
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from boba.auth.config import ProxyAuthConfig
from boba.auth.profiles import ProfileProviders
from boba.auth.roles import HeaderAttribute, RoleProviders
from boba.identity.admission import PrincipalFacts
from boba.identity.errors import AuthenticationError, AuthorizationError
from boba.identity.session import SignInProvider, UserLogin
from boba.identity.signin import ProxyRequest, ProxySignIn, SignedIn, SignInMetadata

__all__ = [
    "AssertionClaim",
    "AssertionRejectedError",
    "HmacProxySignIn",
    "ProxyAssertion",
    "ProxyAssertions",
    "ProxySignature",
]

logger = logging.getLogger(__name__)


class ProxySignature:
    """Подпись proxy-запроса: HMAC-SHA256 по payload() в hex. Одна точка для
    выпуска (тесты, документация) и проверки (провайдер)."""

    def __init__(self, secret: str) -> None:
        self._key = secret.encode()

    def sign(self, request: ProxyRequest) -> str:
        digest = hmac.new(self._key, request.payload().encode(), hashlib.sha256)

        return digest.hexdigest()

    def matches(self, request: ProxyRequest) -> bool:
        return hmac.compare_digest(self.sign(request), request.signature.lower())


class AssertionRejectedError(Exception):
    """Утверждение доверенного бэкенда не принято."""


class AssertionClaim(StrEnum):
    """Имена клеймов и заголовка утверждения (RFC 7523, SEP-990)."""

    TYPE = "oauth-id-jag+jwt"
    ALGORITHM = "HS256"
    ROLES = "roles"


class ProxyAssertion(BaseModel):
    """Утверждение доверенного бэкенда о пользователе: кто выпустил (iss и
    client_id — идентификатор клиента OAuth), о ком (sub — логин), для какого
    сервера авторизации (aud) и ресурса (resource), с какими ролями пользователь
    вошёл в бэкенд. Живёт секунды: iat..exp; jti — против повтора."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    iss: str = Field(min_length=1)
    sub: str = Field(min_length=1)
    aud: str = Field(min_length=1)
    client_id: str = Field(min_length=1)
    resource: str = Field(min_length=1)
    roles: str = ""
    jti: str = Field(min_length=1)
    iat: int
    exp: int


class ProxyAssertions:
    """Выпуск и проверка утверждения общим секретом [auth.proxy].secret.

    Выпускает клиент (чат) на каждый обмен, проверяет сервер авторизации
    сервиса boba-mcp в exchange_identity_assertion. Повтор отсекается памятью
    jti на срок жизни утверждения.
    """

    TTL_SEC: ClassVar[int] = 60

    def __init__(self, secret: str, max_skew_sec: int) -> None:
        self._secret = secret
        self._max_skew_sec = max_skew_sec
        self._seen: dict[str, int] = {}

    def issue(
        self, client_id: str, login: str, roles: str, audience: str, resource: str
    ) -> str:
        now = int(time.time())
        claims = ProxyAssertion(
            iss=client_id,
            sub=login,
            aud=audience,
            client_id=client_id,
            resource=resource,
            roles=roles,
            jti=uuid4().hex,
            iat=now,
            exp=now + self.TTL_SEC,
        )

        return jwt.encode(
            claims.model_dump(mode="json"),
            self._secret,
            algorithm=AssertionClaim.ALGORITHM.value,
            headers={"typ": AssertionClaim.TYPE.value},
        )

    def read(self, assertion: str, client_id: str, audience: str) -> ProxyAssertion:
        """Утверждение клиента client_id для сервера audience; иначе —
        AssertionRejectedError с причиной."""
        # издатель в метаданных и у клиента может отличаться завершающим слэшем
        issuer = audience.rstrip("/")
        try:
            header = jwt.get_unverified_header(assertion)
            raw = jwt.decode(
                assertion,
                self._secret,
                algorithms=[AssertionClaim.ALGORITHM.value],
                audience=[issuer, f"{issuer}/"],
                leeway=self._max_skew_sec,
            )
            claims = ProxyAssertion.model_validate(raw)
        except (jwt.PyJWTError, ValidationError) as exc:
            msg = (
                f"identity assertion of client {client_id!r} for {audience}: "
                f"expected an HS256 JWT under [auth.proxy].secret, got {exc}"
            )
            raise AssertionRejectedError(msg) from exc

        if header.get("typ") != AssertionClaim.TYPE.value:
            msg = (
                f"identity assertion of client {client_id!r}: header typ expects "
                f"{AssertionClaim.TYPE.value!r}, got {header.get('typ')!r}"
            )
            raise AssertionRejectedError(msg)

        if claims.client_id != client_id:
            msg = (
                f"identity assertion of {claims.sub!r}: issued to client "
                f"{claims.client_id!r}, presented by client {client_id!r}"
            )
            raise AssertionRejectedError(msg)

        if claims.exp - claims.iat > self.TTL_SEC:
            msg = (
                f"identity assertion of {claims.sub!r}: lifetime "
                f"{claims.exp - claims.iat}s exceeds {self.TTL_SEC}s"
            )
            raise AssertionRejectedError(msg)

        self._remember(claims)

        return claims

    def _remember(self, claims: ProxyAssertion) -> None:
        now = int(time.time())
        for jti in [jti for jti, until in self._seen.items() if until < now]:
            del self._seen[jti]

        if claims.jti in self._seen:
            msg = (
                f"identity assertion of {claims.sub!r}: jti {claims.jti} is "
                "presented again"
            )
            raise AssertionRejectedError(msg)

        self._seen[claims.jti] = claims.exp + self._max_skew_sec


class HmacProxySignIn(ProxySignIn):
    """Реализация ProxySignIn над конфигом [auth.proxy]: подпись HMAC.

    Порядок: адрес клиента → полнота заголовков → окно времени → подпись →
    роли. Каталог спрашивается только после подписи, чтобы чужой запрос не
    ходил в LDAP. Итог — SignedIn с провайдером ProxyAuth, строку users и
    токен выпускает AuthService.
    """

    def __init__(
        self, config: ProxyAuthConfig, roles: RoleProviders, profiles: ProfileProviders
    ) -> None:
        self._config = config
        self._signature = ProxySignature(config.secret.get_secret_value())
        self._networks = config.networks()
        self._roles = roles
        self._profiles = profiles

    async def sign_in(self, request: ProxyRequest) -> SignedIn:
        self._check_client(request)
        self._check_present(request)
        self._check_timestamp(request)
        self._check_signature(request)

        return await self.admit(request)

    async def admit(self, request: ProxyRequest) -> SignedIn:
        """Допуск по уже подтверждённому запросу: роли и профили. Кто и как
        подтвердил — дело упаковки: подпись заголовков здесь же либо
        утверждение JWT у сервера авторизации, где клиента подтверждает его
        секрет."""
        login = UserLogin.of(request.login)
        facts = self._facts_of(request, login)
        roles = frozenset(await self._roles.admit(facts))
        grant = await self._profiles.granted(facts, roles)

        sign_in = SignInMetadata(
            provider=SignInProvider.PROXY.value,
            roles=roles,
            profiles=grant.granted,
            profile=grant.selected,
        )

        return SignedIn(
            identifier=login.key, display_name=login.display, sign_in=sign_in
        )

    def _check_client(self, request: ProxyRequest) -> None:
        if not self._networks:
            return

        try:
            address = ip_address(request.client)
        except ValueError as exc:
            msg = (
                f"proxy sign-in of {request.login!r}: client address "
                f"{request.client!r} is not an IP while allowed_clients is set"
            )
            raise AuthorizationError(msg) from exc

        for network in self._networks:
            if address in network:
                return

        msg = (
            f"proxy sign-in of {request.login!r}: client {request.client} is "
            f"outside allowed_clients {self._config.allowed_clients}"
        )
        logger.warning("%s", msg)
        raise AuthorizationError(msg)

    def _check_present(self, request: ProxyRequest) -> None:
        headers = self._config.headers
        if not request.login.strip():
            msg = f"proxy sign-in: header {headers.user} is missing or empty"
            raise AuthenticationError(msg)

        if not request.timestamp:
            msg = (
                f"proxy sign-in of {request.login!r}: header {headers.timestamp} "
                "is missing"
            )
            raise AuthenticationError(msg)

        if not request.signature:
            msg = (
                f"proxy sign-in of {request.login!r}: header {headers.signature} "
                "is missing"
            )
            raise AuthenticationError(msg)

    def _check_timestamp(self, request: ProxyRequest) -> None:
        try:
            stamp = int(request.timestamp)
        except ValueError as exc:
            msg = (
                f"proxy sign-in of {request.login!r}: header "
                f"{self._config.headers.timestamp} expects unix seconds, "
                f"got {request.timestamp!r}"
            )
            raise AuthenticationError(msg) from exc

        now = int(time.time())
        skew = abs(now - stamp)
        if skew <= self._config.max_skew_sec:
            return

        msg = (
            f"proxy sign-in of {request.login!r}: timestamp {stamp} is {skew}s "
            f"away from the server clock {now}, max_skew_sec = "
            f"{self._config.max_skew_sec}"
        )
        raise AuthenticationError(msg)

    def _check_signature(self, request: ProxyRequest) -> None:
        if self._signature.matches(request):
            return

        msg = (
            f"proxy sign-in of {request.login!r} from {request.client}: "
            "signature does not match HMAC-SHA256 of "
            "login:timestamp:roles:profiles:profile under [auth.proxy].secret"
        )
        logger.warning("%s", msg)
        raise AuthenticationError(msg)

    @staticmethod
    def _facts_of(request: ProxyRequest, login: UserLogin) -> PrincipalFacts:
        """Факты для провайдеров: логин и значения заголовков как доверенные
        атрибуты, подпись их уже покрыла."""
        return PrincipalFacts(
            login=login.key,
            attributes={
                HeaderAttribute.ROLES.value: request.roles,
                HeaderAttribute.PROFILES.value: request.profiles,
                HeaderAttribute.PROFILE.value: request.profile,
            },
        )
