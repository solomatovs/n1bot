"""SPNEGO-accept по keytab сервиса и захват делегированного тикета входа.

Ошибки:
KeytabError — keytab/SPN сервиса непригодны.
InvalidTokenError — токен клиента битый, просроченный или неполный.
KerberosError — прочие сбои GSSAPI.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import ClassVar

import krb5
from gssapi import Credentials, Name, NameType, SecurityContext
from gssapi.raw.misc import GSSError

from boba.krb.config import (
    AcceptConfig,
    Delegation,
    DelegationMode,
    ForwardedDelegation,
)
from boba.krb.credentials import CcacheLifetime, CcacheRegistry, UserCcache
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
    """accept-сторона SPNEGO: проверяет токен клиента ключом SPN из keytab сервиса.

    В режиме constrained креды берутся на обе стороны (accept + initiate):
    для S4U2Proxy сервису нужен собственный TGT из того же keytab, и MIT
    отдаёт evidence-креды пользователя только такому acceptor'у.
    """

    def __init__(self, config: AcceptConfig, delegation: Delegation) -> None:
        self._config = config
        self._delegation = delegation
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
            return SecurityContext(creds=self._credentials(), usage="accept")
        except GSSError as exc:
            msg = (
                f"spnego accept creds {self._config.service_name} "
                f"from {self._config.keytab}"
            )
            raise KeytabError(f"{msg}: {exc}") from exc

    def _credentials(self) -> Credentials:
        keytab = self._config.keytab.encode()

        if isinstance(self._delegation, ForwardedDelegation):
            name = Name(self._config.service_name, NameType.kerberos_principal)
            return Credentials(name=name, usage="accept", store={b"keytab": keytab})

        # имя не задаётся: SPN не может быть клиентом, ключ учётки берётся из keytab
        return Credentials(
            usage="both",
            store={
                b"keytab": keytab,
                b"client_keytab": keytab,
                b"ccache": self._delegation.service_ccache.encode(),
            },
        )

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
    """Делегированные при логине креды складываются в ccache входа и регистрируются.

    Содержимое ccache сверяется с режимом: forwarded требует TGT пользователя,
    constrained — evidence-тикет и отсутствие TGT пользователя. Несовпадение —
    вход без делегирования, причина в логе.
    """

    LOGIN_BYTES: ClassVar[int] = 18
    """Длина случайной метки входа в байтах."""

    def __init__(
        self,
        registry: CcacheRegistry,
        config: AcceptConfig,
        delegation: Delegation,
    ) -> None:
        self._registry = registry
        self._config = config
        self._delegation = delegation
        self._logger = logging.getLogger(KerberosDelegation.__name__)

    def on_success_authenticated(self, identity: SpnegoIdentity) -> str:
        """Складывает делегированный тикет входа в свой ccache; итог — метка входа.

        Пустая метка — делегированных кредов у входа нет.
        """
        return self._capture(identity, secrets.token_urlsafe(self.LOGIN_BYTES))

    def on_refresh_authenticated(self, identity: SpnegoIdentity, login: str) -> str:
        """Повторный обмен той же сессии: креды ложатся под её метку входа."""
        return self._capture(identity, login)

    def _capture(self, identity: SpnegoIdentity, login: str) -> str:
        if identity.delegated is None:
            self._logger.warning(
                "no delegated_credentials for %s (delegation not permitted in AD)",
                identity.principal,
            )
            return ""

        ccache = self._ccache_of(login)

        try:
            identity.delegated.store(
                store={b"ccache": ccache.encode()},
                usage="initiate",
                overwrite=True,
            )
        except GSSError as exc:
            msg = f"failed to store delegated ccache {ccache}"
            raise KerberosError.of_gss(exc, msg) from exc

        refusal = self.mismatch(ccache, identity.principal, self._registry.mode)
        if refusal:
            self._logger.error(
                "kerberos: delegated credentials of %s rejected: %s",
                identity.principal,
                refusal,
            )
            self._destroy(ccache)
            return ""

        self._registry.register(UserCcache(identity.principal, ccache, login))
        self._logger.info(
            "kerberos: captured delegated credentials (%s) %s -> %s",
            self._registry.mode.value,
            identity.principal,
            ccache,
        )

        return login

    def knows(self, login: str) -> bool:
        """Есть ли у этой метки живой вход: обмен продлевает, а не заводит вход."""
        return self._registry.of_login(login) is not None

    @staticmethod
    def mismatch(ccache: str, principal: str, mode: DelegationMode) -> str:
        """Чем содержимое ccache не подходит режиму; пустая строка — подходит."""
        tgt = CcacheLifetime.tgt(ccache, principal)
        evidence = CcacheLifetime.evidence(ccache, principal)

        if mode is DelegationMode.FORWARDED:
            if tgt == 0:
                return "no forwarded TGT (AD: service not trusted for delegation?)"

            return ""

        if tgt > 0:
            return "a forwarded TGT arrived while constrained delegation is configured"

        if evidence == 0:
            return "no evidence ticket (acceptor credentials without a service TGT?)"

        return ""

    @staticmethod
    def _destroy(ccache: str) -> None:
        try:
            context = krb5.init_context()
            krb5.cc_destroy(context, krb5.cc_resolve(context, ccache.encode()))
        except krb5.Krb5Error:
            return

    def _ccache_of(self, login: str) -> str:
        template = self._delegation.ccache_template

        try:
            return template.format(login=login)
        except (KeyError, IndexError) as exc:
            raise KerberosError(f"bad ccache_template {template!r}: {exc}") from exc
