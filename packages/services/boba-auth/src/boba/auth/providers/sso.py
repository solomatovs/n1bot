"""Вход Kerberos-принципала: личность, роли и профили без SPNEGO I/O.

`SsoSignIn` получает уже проверенные факты от runtime Kerberos adapter и
возвращает общий `SignInResult`. Проверку билета и его шифрование выполняет adapter.

Ошибки:
AuthorizationError — принципал не допущен или не соответствует шаблону.
AuthenticationError — каталог не нашёл пользователя.
ExternalServiceError — каталог недоступен.
InternalServiceError — служебный поиск каталога завершился ошибкой.
"""

from __future__ import annotations

import logging

from boba.auth.config import KerberosAuthConfig
from boba.auth.providers.profiles import ProfileProviders
from boba.auth.providers.result import SignInDetails, SignInResultFactory
from boba.auth.providers.roles import RoleProviders
from boba.identity.admission import PrincipalFacts
from boba.identity.authentication import RenewalKind
from boba.identity.context import CredentialAbsence, DelegatedTicket, NoUserCredential
from boba.identity.errors import AuthorizationError
from boba.identity.session import LoginTemplate, UserLogin
from boba.identity.signin import SignInResult
from boba.identity.sso import SsoAdmission
from boba.toolkit.template import TemplateError

__all__ = ["SsoSignIn"]

logger = logging.getLogger(__name__)


class SsoSignIn(SsoAdmission):
    """Создаёт общий результат входа из проверенного Kerberos-принципала.

    Runtime adapter передаёт факты SPNEGO и запечатанный делегированный билет;
    этот класс применяет правила ролей и профилей и не знает о Kerberos I/O.
    """

    def __init__(
        self,
        config: KerberosAuthConfig,
        roles: RoleProviders,
        profiles: ProfileProviders,
    ) -> None:
        self._config = config
        self._roles = roles
        self._signed_in = SignInResultFactory(roles, profiles)

    @property
    def config(self) -> KerberosAuthConfig:
        return self._config

    async def signed_in(self, facts: PrincipalFacts, sealed: str) -> SignInResult:
        """Итог входа: каноническая личность, роли БД, профили и шифротекст билета."""
        username = self._username_from_principal(
            self._config.principal_format, facts.principal
        )
        login = UserLogin.of(username)
        identity = self._config.identity.identity(login.key)
        if not sealed:
            logger.warning(
                "kerberos: sign-in of %s carries no delegated ticket", facts.principal
            )

        return await self._signed_in.create(
            SignInDetails(
                provider=self._config.PROVIDER,
                renewal=RenewalKind.KERBEROS if sealed else RenewalKind.REAUTHENTICATE,
                identity=identity,
                facts=facts,
                credential=(
                    DelegatedTicket(principal=facts.principal, sealed=sealed)
                    if sealed
                    else NoUserCredential(code=CredentialAbsence.KERBEROS_NOT_DELEGATED)
                ),
            )
        )

    async def roles_of(self, facts: PrincipalFacts) -> list[str]:
        """Роли по проверенным фактам SPNEGO; также повторно проверяются при refresh."""
        username = self._username_from_principal(
            self._config.principal_format, facts.principal
        )
        identity = self._config.identity.identity(UserLogin.of(username).key)

        return await self._roles.admit(facts, identity)

    @staticmethod
    def _username_from_principal(principal_format: str, principal: str) -> str:
        """user@REALM | DOMAIN\\user -> логин по шаблону с `{username}`."""
        try:
            return LoginTemplate.username_of(principal_format, principal)
        except TemplateError as exc:
            message = (
                f"principal {principal!r} does not match the configured "
                f"principal_format {principal_format!r}: {exc}"
            )
            raise AuthorizationError(message) from exc
