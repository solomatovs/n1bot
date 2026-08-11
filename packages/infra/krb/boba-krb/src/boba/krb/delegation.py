"""SPNEGO-accept по keytab сервиса и получение тикета к бэкенду от имени пользователя.

Ошибки: KeytabError — keytab/SPN сервиса непригодны;
InvalidTokenError — токен клиента битый, просроченный или неполный;
CredentialsExpiredError — тикет пользователя истёк;
DelegationNotPermittedError — делегирование запрещено политикой AD;
KerberosError — прочие сбои GSSAPI.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass, field

from gssapi import Credentials, Name, NameType, SecurityContext
from gssapi.raw.misc import GSSError

from boba.krb.config import AcceptConfig, DelegationConfig
from boba.krb.credentials import CcacheRegistry, UserCcache
from boba.krb.errors import InvalidTokenError, KerberosError, KeytabError
from boba.krb.pac import PacGroupSids

__all__ = ["KerberosDelegation", "SpnegoAcceptor", "SpnegoIdentity"]


@dataclass(frozen=True)
class SpnegoIdentity:
    """Результат SPNEGO-accept: кто вошёл, его группы и делегированные им креды."""

    principal: str
    group_sids: Sequence[str] = field(default_factory=tuple)
    delegated: Credentials | None = None


class SpnegoAcceptor:
    """accept-сторона SPNEGO: проверяет токен клиента ключом SPN из keytab сервиса."""

    def __init__(self, config: AcceptConfig) -> None:
        self._config = config
        self._logger = logging.getLogger(SpnegoAcceptor.__name__)

    def accept(self, token: bytes) -> SpnegoIdentity:
        """Проверяет токен и возвращает принципал клиента с его группами."""
        ctx = self._context()

        try:
            ctx.step(token)
        except GSSError as exc:
            raise InvalidTokenError(f"spnego step failed: {exc}") from exc

        if not ctx.complete:
            msg = "spnego context incomplete (multi-leg not supported)"
            raise InvalidTokenError(msg)

        principal = str(ctx.initiator_name)

        return SpnegoIdentity(
            principal=principal,
            group_sids=self._group_sids(principal, ctx),
            delegated=ctx.delegated_creds,
        )

    async def accept_async(self, token: bytes) -> SpnegoIdentity:
        """accept() без блокировки event loop."""
        return await asyncio.to_thread(self.accept, token)

    def _context(self) -> SecurityContext:
        """accept-контекст на ключе SPN; сбой здесь — проблема keytab/SPN сервиса."""
        try:
            name = Name(self._config.service_name, NameType.kerberos_principal)
            creds = Credentials(
                name=name,
                usage="accept",
                store={b"keytab": self._config.keytab},
            )
            return SecurityContext(creds=creds, usage="accept")
        except GSSError as exc:
            msg = (
                f"spnego accept creds {self._config.service_name} "
                f"from {self._config.keytab}"
            )
            raise KeytabError(f"{msg}: {exc}") from exc

    def _group_sids(self, principal: str, ctx: SecurityContext) -> Sequence[str]:
        """SID-ы групп из PAC; пустой список, если PAC нет или он не разобрался."""
        try:
            sids = PacGroupSids.of_context(ctx)
        except ValueError as exc:
            self._logger.error(
                "kerberos: PAC logon-info parse failed [principal=%s]: %s",
                principal,
                exc,
            )
            return ()

        if not sids:
            self._logger.warning(
                "kerberos: no PAC group SIDs [principal=%s]", principal
            )

        return sids


class KerberosDelegation:
    "Стратегия по AD-учётке: захваченный при логине TGT либо S4U по keytab сервиса"

    def __init__(
        self,
        registry: CcacheRegistry,
        config: AcceptConfig,
        delegation: DelegationConfig,
    ) -> None:
        self._registry = registry
        self._config = config
        self._delegation = delegation
        self._logger = logging.getLogger(KerberosDelegation.__name__)

    @staticmethod
    def sanitize_principal(principal: str) -> str:
        return re.sub(r"[^\w.@-]", "_", principal)

    def captured(self, username: str) -> bool:
        "Есть ли захваченный при логине ccache пользователя (форварднутый TGT)."
        return self._registry.of(username) is not None

    def on_success_authenticated(self, identity: SpnegoIdentity) -> None:
        """Складывает делегированный тикет пользователя в его ccache."""
        if identity.delegated is None:
            self._logger.warning(
                "no delegated_credentials for %s (delegation not permitted in AD)",
                identity.principal,
            )
            return

        ccache = self._ccache_of(identity.principal)

        try:
            identity.delegated.store(
                store={b"ccache": ccache.encode()},
                usage="initiate",
                overwrite=True,
            )
        except GSSError as exc:
            msg = f"failed to store delegated ccache {ccache}"
            raise KerberosError.of_gss(exc, msg) from exc

        self._registry.register(UserCcache(identity.principal, ccache))
        self._logger.info(
            "kerberos: captured delegated ticket %s -> %s", identity.principal, ccache
        )

    def _ccache_of(self, principal: str) -> str:
        template = self._delegation.ccache_template
        safe_principal = self.sanitize_principal(principal)

        try:
            return template.format(principal=safe_principal)
        except (KeyError, IndexError) as exc:
            raise KerberosError(f"bad ccache_template {template!r}: {exc}") from exc

    async def credentials_for(self, username: str, target_spn: str) -> bytes:
        """AP-REQ к target_spn от имени пользователя: делегированный тикет или S4U."""
        credentials = self._registry.of(username)

        if credentials is None:
            return await asyncio.to_thread(self._s4u_token, username, target_spn)

        await credentials.ensure_async()

        return await asyncio.to_thread(self._init_token, credentials.ccache, target_spn)

    @staticmethod
    def _init_token(ccache: str, target_spn: str) -> bytes:
        try:
            creds = Credentials(usage="initiate", store={b"ccache": ccache.encode()})
            target = Name(target_spn, NameType.kerberos_principal)
            ctx = SecurityContext(name=target, creds=creds, usage="initiate")
            token = ctx.step()
        except GSSError as exc:
            raise KerberosError.of_gss(exc, f"initiate to {target_spn}") from exc

        # initiate обязан выдать AP-REQ для бэкенда; пустой токен = слать нечего
        if not token:
            msg = "gss step returned empty token on initiate side"
            raise KerberosError(msg)

        return token

    def _s4u_token(self, username: str, target_spn: str) -> bytes:
        try:
            service = Credentials(
                name=Name(self._config.service_name, NameType.kerberos_principal),
                usage="both",
                store={b"keytab": self._config.keytab},
            )
            user = Name(username, NameType.kerberos_principal)
            user_creds = service.impersonate(user)
            target = Name(target_spn, NameType.kerberos_principal)
            # initiate этими creds → KDC делает S4U2Proxy и проверяет whitelist
            ctx = SecurityContext(name=target, creds=user_creds, usage="initiate")
            token = ctx.step()
        except GSSError as exc:
            msg = f"s4u {username} to {target_spn}"
            raise KerberosError.of_gss(exc, msg) from exc

        # initiate обязан выдать AP-REQ для бэкенда; пустой токен = слать нечего
        if not token:
            msg = "gss step returned empty token on initiate side (S4U)"
            raise KerberosError(msg)

        return token
