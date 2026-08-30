"""SSO через Kerberos/SPNEGO, общее для приложений: обмен, допуск, билет входа.

Приложение вешает обмен на свой URL: SpnegoGate отдаёт исход (вызов, вход,
отказ), а JWT, cookie и страницу логина делает вызывающий.

Ошибки:
AuthorizationError — принципал не допущен (исключён или без ролей).
ExternalServiceError — креды kerberos истекли или недоступен LDAP.
InternalServiceError — keytab/SPN/делегирование/конфиг непригодны.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence

from boba.auth.config import KerberosAuthConfig, KerberosRolesInLdapConfig
from boba.auth.signin import ADDirectory
from boba.identity.admission import PrincipalFacts, RoleRules
from boba.identity.context import DelegatedTicket
from boba.identity.directory import (
    ADUserEntry,
    LDAPError,
    LDAPInvalidCredentialsError,
    LDAPServerUnavailableError,
    LDAPUserNotFoundError,
)
from boba.identity.errors import (
    AuthenticationError,
    AuthorizationError,
    BaseError,
    ExternalServiceError,
    InternalServiceError,
)
from boba.identity.session import (
    LoginTemplate,
    SignInProvider,
    UserLogin,
)
from boba.identity.signin import SignedIn, SignInMetadata
from boba.identity.sso import (
    SpnegoExchange,
    SsoAdmission,
    SsoChallenge,
    SsoRefused,
    SsoRequest,
    SsoSigned,
)
from boba.kerberos import (
    CredentialsExpiredError,
    DelegationNotPermittedError,
    InvalidTokenError,
    KerberosError,
    KeytabError,
    NegotiateToken,
)
from boba.krb import SpnegoAcceptor, SpnegoIdentity, TicketCapture
from boba.krb.seal import SsoTickets, TicketSealer
from boba.toolkit.template import TemplateError

__all__ = [
    "KerberosErrorToDomain",
    "KerberosRolesInLdapProvider",
    "SpnegoGate",
    "SsoSignIn",
]


class KerberosErrorToDomain:
    "Классификация ошибок kerberos-слоя в доменную ошибку."

    @staticmethod
    def map(e: KerberosError) -> BaseError:
        # нужен повторный логин — не наша вина
        if isinstance(e, CredentialsExpiredError):
            return ExternalServiceError(
                "kerberos", "Kerberos credentials expired, please re-login"
            )

        # делегирование запрещено политикой (msDS-AllowedToDelegateTo) — конфиг AD
        if isinstance(e, DelegationNotPermittedError):
            return InternalServiceError(
                internal_detail=f"kerberos delegation not permitted: {e}",
                user_detail=None,
            )

        # keytab/SPN сервиса непригодны — наша сторона
        if isinstance(e, KeytabError):
            return InternalServiceError(
                internal_detail=f"kerberos keytab problem: {e}",
                user_detail=None,
            )

        return InternalServiceError(
            internal_detail=f"kerberos error: {e}",
            user_detail=None,
        )


class KerberosRolesInLdapProvider:
    """Факты о принципале из каталога: DN, sAMAccountName и группы по UPN."""

    def __init__(self, config: KerberosRolesInLdapConfig):
        self._config = config
        self._ad = ADDirectory

    async def request(self, principal: str) -> ADUserEntry:
        search_filter = f"(userPrincipalName={principal})"

        try:
            user_dn, samaccountname, member_of = await asyncio.to_thread(
                self._ad.fetch_userdn_samaccountname_member_of,
                server=self._config.server,
                bind_dn=self._config.bind_dn,
                bind_password=self._config.bind_password.get_secret_value(),
                search_base=self._config.base_dn,
                search_filter=search_filter,
            )

            return ADUserEntry(
                dn=user_dn,
                samaccountname=samaccountname,
                member_of=member_of,
            )
        except LDAPUserNotFoundError as e:
            raise AuthenticationError("User is not registered") from e
        except LDAPServerUnavailableError as e:
            raise ExternalServiceError(
                "ldap",
                "LDAP service is unavailable, please try again later",
            ) from e
        except LDAPInvalidCredentialsError as e:
            raise InternalServiceError(
                internal_detail=f"ldap service bind rejected: {e}",
                user_detail=None,
            ) from e
        except LDAPError as e:
            raise InternalServiceError(
                internal_detail=f"ldap error: {e}", user_detail=None
            ) from e


class SsoSignIn(SsoAdmission):
    """Вход по SPNEGO-личности: роли по правилам конфига и запечатанный билет."""

    def __init__(self, config: KerberosAuthConfig, secret: str) -> None:
        if not secret:
            msg = "sso secret is empty: it seals the sign-in ticket"
            raise ValueError(msg)

        self._config = config
        self.acceptor = SpnegoAcceptor(config.accept, config.delegation)
        self.capture = TicketCapture(config.delegation)
        self.sealer = TicketSealer(secret)
        self.krb5_config = config.delegation.krb5_config
        self._logger = logging.getLogger(SsoSignIn.__name__)
        self._rules: RoleRules = config.rules()
        self._directory: KerberosRolesInLdapProvider | None = None
        if ldap_roles := config.ldap_roles:
            self._directory = KerberosRolesInLdapProvider(ldap_roles)

    @property
    def config(self) -> KerberosAuthConfig:
        return self._config

    def tickets(self) -> SsoTickets:
        """Открыватель билетов входа для обвязок инструментов."""
        return SsoTickets(sealer=self.sealer, krb5_config=self.krb5_config)

    @staticmethod
    def facts_of(identity: SpnegoIdentity) -> PrincipalFacts:
        """Факты SPNEGO-личности: принципал, SID-ы групп и разобрался ли PAC."""
        return PrincipalFacts(
            principal=identity.principal,
            group_sids=tuple(identity.group_sids),
            pac_parsed=identity.pac_parsed,
        )

    @staticmethod
    def _username_from_principal(principal_format: str, principal: str) -> str:
        """user@REALM | DOMAIN\\user -> sAMAccountName по шаблону с {username}."""
        try:
            return LoginTemplate.username_of(principal_format, principal)
        except TemplateError as exc:
            raise InternalServiceError(
                internal_detail=(
                    f"principal {principal!r} does not match "
                    f"principal_format {principal_format!r}: {exc}"
                ),
                user_detail=None,
            ) from exc

    def sealed_of(self, identity: SpnegoIdentity) -> str:
        """Запечатанный билет входа; пустая строка — делегирования у входа нет."""
        ticket = self.capture.capture(identity)
        if ticket is None:
            return ""

        return self.sealer.seal(ticket)

    async def signed_in(self, identity: SpnegoIdentity, sealed: str) -> SignedIn:
        """Итог входа: логин из принципала, роли по допуску, билет в metadata."""
        roles = await self.roles_of(self.facts_of(identity))
        sign_in = self._sign_in_of(identity.principal, sealed, roles)

        username = self._username_from_principal(
            self._config.principal_format, identity.principal
        )
        login = UserLogin.of(username)

        return SignedIn(
            identifier=login.key, display_name=login.display, sign_in=sign_in
        )

    async def roles_of(self, facts: PrincipalFacts) -> list[str]:
        """Роли по фактам SPNEGO плюс фактам каталога, если он настроен.

        Зовётся и при входе, и при повторном обмене: запрет в AD должен
        отсекать обмен так же, как отсёк бы новый вход.
        """
        if self._directory is not None:
            entry = await self._directory.request(facts.principal)
            facts = facts.model_copy(
                update={
                    "login": entry.samaccountname,
                    "dn": entry.dn,
                    "member_of": tuple(entry.member_of),
                }
            )

        return self._rules.admit(facts)

    def _sign_in_of(
        self, principal: str, sealed: str, roles: Sequence[str]
    ) -> SignInMetadata:
        """Metadata входа: провайдер, принципал, роли и запечатанный билет."""
        if not sealed:
            # без билета сессия останется без делегированных кредов: причина в логе выше
            self._logger.warning(
                "kerberos: sign-in of %s carries no delegated ticket", principal
            )

        return SignInMetadata(
            provider=SignInProvider.KERBEROS.value,
            principal=principal,
            sealed_ticket=sealed,
            roles=frozenset(roles),
        )


class SpnegoGate(SpnegoExchange):
    """SPNEGO-обмен на URL приложения: вход и молчаливое обновление билета сессии."""

    def __init__(self, sign_in: SsoSignIn) -> None:
        self._sign_in = sign_in
        self._logger = logging.getLogger(SpnegoGate.__name__)

    @property
    def sign_in(self) -> SsoSignIn:
        return self._sign_in

    async def handshake(self, request: SsoRequest) -> SsoChallenge | SsoSigned:
        """Вход: токен из Authorization → личность → допуск → SignedIn."""
        client = request.client
        found = NegotiateToken.of(request.authorization)
        if isinstance(found, str):
            # начало handshake токена не несёт — это не ошибка
            return self._challenge(request, found, logging.INFO)

        try:
            identity = await self._accepted(found, client, "accept")
        except InvalidTokenError as e:
            return self._challenge(request, str(e), logging.WARNING)

        sealed = self._sign_in.sealed_of(identity)
        signed = await self._sign_in.signed_in(identity, sealed)
        self._logger.info(
            "kerberos: sign-in ticket sealed [%d chars] [principal=%s]",
            len(sealed),
            identity.principal,
        )

        return SsoSigned(signed=signed, principal=identity.principal)

    async def refresh(
        self, request: SsoRequest, session: DelegatedTicket | None
    ) -> SsoChallenge | SsoRefused | SsoSigned:
        """Повторный SPNEGO живой сессии: свежий билет под тем же принципалом."""
        client = request.client
        refused = self._refresh_allowed(request, session)
        if refused is not None:
            self._logger.warning("kerberos refresh refused: %s", refused.reason)
            return refused

        found = NegotiateToken.of(request.authorization)
        if isinstance(found, str):
            return self._challenge(request, found, logging.INFO)

        try:
            identity = await self._accepted(found, client, "refresh")
        except InvalidTokenError as e:
            return self._challenge(request, str(e), logging.INFO)

        outcome = await self._refreshed(identity, session, client)
        if isinstance(outcome, SsoRefused):
            self._logger.warning("kerberos refresh refused: %s", outcome.reason)

        return outcome

    @staticmethod
    def _refresh_allowed(
        request: SsoRequest, session: DelegatedTicket | None
    ) -> SsoRefused | None:
        if not request.own_request:
            # заголовок ставит только свой fetch: чужая страница обмен не запустит
            reason = f"refresh without its own header [{request.client}]"
            return SsoRefused(reason=reason)

        if session is None:
            return SsoRefused(reason="request carries no signed sign-in")

        return None

    async def _refreshed(
        self, identity: SpnegoIdentity, session: DelegatedTicket | None, client: str
    ) -> SsoRefused | SsoSigned:
        if session is None:
            return SsoRefused(reason="request carries no signed sign-in")

        if identity.principal != session.principal:
            reason = (
                f"refresh token of {identity.principal} does not match the "
                f"session of {session.principal}"
            )
            return SsoRefused(reason=reason)

        try:
            await self._sign_in.roles_of(self._sign_in.facts_of(identity))
        except AuthorizationError:
            return SsoRefused(reason=f"{identity.principal} is no longer admitted")

        sealed = self._sign_in.sealed_of(identity)
        if not sealed:
            reason = f"no delegated credentials for {session.principal}"
            return SsoRefused(reason=reason)

        signed = await self._sign_in.signed_in(identity, sealed)
        self._logger.info(
            "kerberos: refreshed sign-in ticket [principal=%s] [client=%s]",
            identity.principal,
            client,
        )

        return SsoSigned(signed=signed, principal=identity.principal)

    async def _accepted(self, token: bytes, client: str, stage: str) -> SpnegoIdentity:
        try:
            identity = await self._sign_in.acceptor.accept_async(token)
        except InvalidTokenError:
            raise
        except KerberosError as e:
            self._logger.exception(
                "kerberos: spnego %s failed (keytab/SPN) [client=%s]", stage, client
            )
            raise KerberosErrorToDomain.map(e) from e

        self._logger.info(
            "kerberos authenticated [principal=%s] [client=%s]",
            identity.principal,
            client,
        )

        return identity

    def _challenge(self, request: SsoRequest, reason: str, level: int) -> SsoChallenge:
        self._logger.log(
            level, "kerberos challenge [client=%s]: %s", request.client, reason
        )

        return SsoChallenge(reason=reason, level=level)
