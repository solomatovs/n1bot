"""Вход по HMAC-подписанным заголовкам доверенного бэкенда.

Проверяет подпись, окно времени и адрес клиента; роли и профили определяют
провайдеры допуска. Результат сохраняет общий SessionService.

Ошибки:
AuthenticationError — неверные подпись, время или обязательные заголовки.
AuthorizationError — клиент либо пользователь не допущен.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import time
from ipaddress import ip_address

from boba.auth.config import ProxyAuthConfig
from boba.auth.providers.profiles import ProfileProviders
from boba.auth.providers.result import SignInDetails, SignInResultFactory
from boba.auth.providers.roles import HeaderAttribute, RoleProviders
from boba.identity.admission import PrincipalFacts
from boba.identity.errors import AuthenticationError, AuthorizationError
from boba.identity.principal import UserIdentity
from boba.identity.session import UserLogin
from boba.identity.signin import ProxyRequest, ProxySignIn, SignInResult

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


class HmacProxySignIn(ProxySignIn):
    """Реализация ProxySignIn над конфигом [auth.proxy]: подпись HMAC.

    Порядок: адрес клиента → полнота заголовков → окно времени → подпись →
    роли. Каталог спрашивается только после подписи, чтобы чужой запрос не
    ходил в LDAP. Итог — SignInResult с провайдером ProxyAuth, строку users и
    сохраняет SessionService, токен выпускает адаптер приложения.
    """

    def __init__(
        self, config: ProxyAuthConfig, roles: RoleProviders, profiles: ProfileProviders
    ) -> None:
        self._config = config
        self._signature = ProxySignature(config.secret.get_secret_value())
        self._networks = config.networks()
        self._signed_in = SignInResultFactory(roles, profiles)

    async def sign_in(self, request: ProxyRequest) -> SignInResult:
        if request.identity is not None:
            msg = (
                f"proxy header sign-in of {request.login!r}: identity is selected "
                "by server configuration, not by unsigned request fields"
            )
            raise AuthenticationError(msg)

        self._check_client(request)
        self._check_present(request)
        self._check_timestamp(request)
        self._check_signature(request)

        return await self.admit(request)

    async def admit(self, request: ProxyRequest) -> SignInResult:
        """Допуск по уже подтверждённому запросу: роли и профили. Кто и как
        подтвердил — дело упаковки: подпись заголовков здесь же либо
        утверждение JWT у сервера авторизации, где клиента подтверждает его
        секрет."""
        login = UserLogin.of(request.login)
        identity = self._identity_of(request, login)
        facts = self._facts_of(request, login)
        return await self._signed_in.create(
            SignInDetails(
                provider=self._config.PROVIDER,
                identity=identity,
                facts=facts,
            )
        )

    def _identity_of(self, request: ProxyRequest, login: UserLogin) -> UserIdentity:
        identity = request.identity
        if identity is None:
            return self._config.identity.identity(login.key)

        if identity.login != login.key:
            msg = (
                f"proxy admission of {login.key!r}: verified identity names "
                f"login {identity.login!r}, expected the same user"
            )
            raise AuthenticationError(msg)

        return identity

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
