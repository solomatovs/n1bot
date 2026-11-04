"""Инфраструктурный SPNEGO-обмен и шифрование билета входа для приложений.

`SpnegoGate` проверяет токен и запечатывает делегированный билет через
`boba-krb`, затем передаёт проверенные факты в `boba-auth.SsoSignIn`.

Ошибки:
AuthorizationError — принципал не допущен (исключён или без ролей).
ExternalServiceError — креды kerberos истекли или недоступен LDAP.
InternalServiceError — keytab/SPN/делегирование/конфиг непригодны.
"""

from __future__ import annotations

import logging

from boba.auth.sso import SsoSignIn
from boba.identity.admission import PrincipalFacts
from boba.identity.context import DelegatedTicket
from boba.identity.errors import (
    AuthorizationError,
    BaseError,
    ExternalServiceError,
    InternalServiceError,
)
from boba.identity.signin import SignedIn
from boba.identity.sso import (
    SessionRefresh,
    SpnegoExchange,
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
from boba.krb import (
    SpnegoAcceptor,
    SpnegoIdentity,
    TicketCapture,
)
from boba.krb.seal import TicketSealer

__all__ = [
    "KerberosErrorToDomain",
    "SpnegoGate",
]


class KerberosErrorToDomain:
    """Преобразует ошибки Kerberos runtime в ошибки общего auth-контракта."""

    @staticmethod
    def map(e: KerberosError, action: str) -> BaseError:
        # нужен повторный логин — не наша вина
        if isinstance(e, CredentialsExpiredError):
            message = f"{action}: Kerberos credentials expired, please re-login: {e}"
            return ExternalServiceError("kerberos", message)

        # делегирование запрещено политикой (msDS-AllowedToDelegateTo) — конфиг AD
        if isinstance(e, DelegationNotPermittedError):
            return InternalServiceError(
                internal_detail=f"{action}: kerberos delegation not permitted: {e}",
                user_detail=None,
            )

        # keytab/SPN сервиса непригодны — наша сторона
        if isinstance(e, KeytabError):
            return InternalServiceError(
                internal_detail=f"{action}: kerberos keytab or SPN unusable: {e}",
                user_detail=None,
            )

        return InternalServiceError(
            internal_detail=f"{action}: kerberos error: {e}",
            user_detail=None,
        )


class SpnegoGate(SpnegoExchange, SessionRefresh):
    """SPNEGO-обмен на URL приложения: вход и молчаливое обновление билета сессии."""

    def __init__(self, sign_in: SsoSignIn, secret: str) -> None:
        if not secret:
            msg = (
                "SPNEGO adapter: [session].auth_secret is required to seal "
                "the sign-in ticket, got an empty string"
            )
            raise ValueError(msg)

        self._sign_in = sign_in
        config = sign_in.config
        self._acceptor = SpnegoAcceptor(config.accept, config.delegation)
        self._capture = TicketCapture(config.delegation)
        self._sealer = TicketSealer(secret)
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

        facts = self._facts_of(identity)
        sealed = self._sealed_of(identity)
        signed = await self._sign_in.signed_in(facts, sealed)
        self._logger.info(
            "kerberos: sign-in ticket sealed [%d chars] [principal=%s]",
            len(sealed),
            identity.principal,
        )

        return SsoSigned(signed=signed, principal=identity.principal)

    async def renew(
        self, request: SsoRequest, signed: SignedIn
    ) -> SsoChallenge | SsoRefused | SsoSigned:
        return await self.refresh(request, signed.sign_in.ticket())

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
            reason = (
                f"kerberos refresh from [{request.client}]: request carries no "
                "delegated sign-in ticket"
            )
            return SsoRefused(reason=reason)

        return None

    async def _refreshed(
        self, identity: SpnegoIdentity, session: DelegatedTicket | None, client: str
    ) -> SsoRefused | SsoSigned:
        if session is None:
            reason = (
                f"kerberos refresh from [{client}]: request carries no "
                "delegated sign-in ticket"
            )
            return SsoRefused(reason=reason)

        if identity.principal != session.principal:
            reason = (
                f"refresh token of {identity.principal} does not match the "
                f"session of {session.principal}"
            )
            return SsoRefused(reason=reason)

        try:
            facts = self._facts_of(identity)
            await self._sign_in.roles_of(facts)
        except AuthorizationError as exc:
            reason = f"{identity.principal} is no longer admitted: {exc}"
            return SsoRefused(reason=reason)

        sealed = self._sealed_of(identity)
        if not sealed:
            reason = (
                f"kerberos refresh of {session.principal}: the SPNEGO token "
                "carries no delegated credentials"
            )
            return SsoRefused(reason=reason)

        facts = self._facts_of(identity)
        signed = await self._sign_in.signed_in(facts, sealed)
        self._logger.info(
            "kerberos: refreshed sign-in ticket [principal=%s] [client=%s]",
            identity.principal,
            client,
        )

        return SsoSigned(signed=signed, principal=identity.principal)

    async def _accepted(self, token: bytes, client: str, stage: str) -> SpnegoIdentity:
        try:
            identity = await self._acceptor.accept_async(token)
        except InvalidTokenError:
            raise
        except KerberosError as e:
            self._logger.exception(
                "kerberos: spnego %s of the token from [client=%s] failed: %s",
                stage,
                client,
                e,
            )
            action = f"spnego {stage} of the token from [{client}]"
            raise KerberosErrorToDomain.map(e, action) from e

        self._logger.info(
            "kerberos authenticated [principal=%s] [client=%s]",
            identity.principal,
            client,
        )

        return identity

    def _sealed_of(self, identity: SpnegoIdentity) -> str:
        ticket = self._capture.capture(identity)
        if ticket is None:
            return ""

        return self._sealer.seal(ticket)

    @staticmethod
    def _facts_of(identity: SpnegoIdentity) -> PrincipalFacts:
        return PrincipalFacts(
            principal=identity.principal,
            group_sids=tuple(identity.group_sids),
            pac_parsed=identity.pac_parsed,
        )

    def _challenge(self, request: SsoRequest, reason: str, level: int) -> SsoChallenge:
        self._logger.log(
            level, "kerberos challenge [client=%s]: %s", request.client, reason
        )

        return SsoChallenge(reason=reason, level=level)
