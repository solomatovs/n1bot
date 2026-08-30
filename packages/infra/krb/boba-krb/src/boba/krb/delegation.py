"""SPNEGO-accept по keytab сервиса и захват делегированного билета входа значением.

Ошибки:
KeytabError — keytab/SPN сервиса непригодны.
InvalidTokenError — токен клиента битый, просроченный или неполный.
KerberosError — прочие сбои GSSAPI.
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import ClassVar

import krb5
from gssapi import Credentials, Name, NameType, SecurityContext
from gssapi.raw.misc import GSSError

from boba.connections.kerberos import (
    AcceptConfig,
    Delegation,
    DelegationMode,
    ForwardedDelegation,
    SignInTicket,
)
from boba.krb.credentials import CcacheLifetime, KerberosEnv
from boba.krb.errors import GssErrors, InvalidTokenError, KeytabError
from boba.krb.pac import PacGroupSids

__all__ = ["SpnegoAcceptor", "SpnegoIdentity", "TicketCapture"]


@dataclass(frozen=True)
class PacGroups:
    """Группы из PAC: SID-ы и признак, что PAC разобрался."""

    sids: Sequence[str]
    parsed: bool


@dataclass(frozen=True)
class SpnegoIdentity:
    """Результат SPNEGO-accept: кто вошёл, его группы и делегированные им креды."""

    principal: str
    group_sids: Sequence[str] = field(default_factory=tuple)
    delegated: Credentials | None = None
    pac_parsed: bool = True
    """False — PAC не разобрался: группы неизвестны, исключения по SID не проверить."""


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
        """Проверяет токен и возвращает принципал клиента с его группами.

        Весь обмен идёт под krb5.conf режима делегирования: в constrained
        пустой ccache сервиса заставляет libkrb5 идти в KDC за собственным
        TGT, а адрес KDC берётся только из конфига.
        """
        values = {KerberosEnv.CONFIG: self._delegation.krb5_config}

        with KerberosEnv.applied(values):
            return self._accepted(token)

    def _accepted(self, token: bytes) -> SpnegoIdentity:
        ctx = self._context()

        try:
            ctx.step(token)
        except GSSError as exc:
            raise InvalidTokenError(f"spnego step failed: {exc}") from exc

        if not ctx.complete:
            msg = "spnego context incomplete (multi-leg not supported)"
            raise InvalidTokenError(msg)

        principal = str(ctx.initiator_name)
        groups = self._group_sids(principal, ctx)

        return SpnegoIdentity(
            principal=principal,
            group_sids=groups.sids,
            delegated=ctx.delegated_creds,
            pac_parsed=groups.parsed,
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

    def _group_sids(self, principal: str, ctx: SecurityContext) -> PacGroups:
        """SID-ы групп из PAC; parsed=False — PAC не разобрался, группы неизвестны."""
        try:
            sids = PacGroupSids.of_context(ctx)
        except ValueError as exc:
            self._logger.error(
                "kerberos: PAC logon-info parse failed [principal=%s]: %s",
                principal,
                exc,
            )
            return PacGroups(sids=(), parsed=False)

        if not sids:
            self._logger.warning(
                "kerberos: no PAC group SIDs [principal=%s]", principal
            )

        return PacGroups(sids=tuple(sids), parsed=True)


class TicketCapture:
    """Делегированные при логине креды -> SignInTicket: ccache читается в память.

    Содержимое сверяется с режимом: forwarded требует TGT пользователя,
    constrained — evidence-тикет и отсутствие TGT пользователя. Несовпадение —
    вход без делегирования, причина в логе. Файл живёт только внутри захвата.
    """

    TEMP_PREFIX: ClassVar[str] = "krb5cc_signin_"

    def __init__(self, delegation: Delegation) -> None:
        self._delegation = delegation
        self._logger = logging.getLogger(TicketCapture.__name__)

    @property
    def mode(self) -> DelegationMode:
        return DelegationMode(self._delegation.mode)

    def capture(self, identity: SpnegoIdentity) -> SignInTicket | None:
        """Билет входа; None — креды не пришли или не подходят режиму."""
        delegated = identity.delegated
        if delegated is None:
            self._logger.warning(
                "no delegated_credentials for %s (delegation not permitted in AD)",
                identity.principal,
            )
            return None

        descriptor, path = tempfile.mkstemp(prefix=self.TEMP_PREFIX)
        os.close(descriptor)
        ccache = f"FILE:{path}"

        try:
            return self._captured(identity.principal, delegated, ccache)
        finally:
            self._destroy(ccache)
            self._unlink(path)

    def _captured(
        self, principal: str, delegated: Credentials, ccache: str
    ) -> SignInTicket | None:
        try:
            delegated.store(
                store={b"ccache": ccache.encode()},
                usage="initiate",
                overwrite=True,
            )
        except GSSError as exc:
            msg = f"failed to store delegated ccache {ccache}"
            raise GssErrors.of(exc, msg) from exc

        refusal = self.mismatch(ccache, principal, self.mode)
        if refusal:
            self._logger.error(
                "kerberos: delegated credentials of %s rejected: %s",
                principal,
                refusal,
            )
            return None

        lifetime = self._lifetime(ccache, principal)
        if lifetime == 0:
            self._logger.error(
                "kerberos: delegated credentials of %s already expired", principal
            )
            return None

        with open(ccache.removeprefix("FILE:"), "rb") as source:
            data = source.read()

        self._logger.info(
            "kerberos: captured delegated credentials (%s) %s, %d bytes, %ds left",
            self.mode.value,
            principal,
            len(data),
            lifetime,
        )

        return SignInTicket(
            principal=principal,
            mode=self.mode,
            ccache=data,
            expires_at=int(time.time()) + lifetime,
        )

    def _lifetime(self, ccache: str, principal: str) -> int:
        if self.mode is DelegationMode.FORWARDED:
            return CcacheLifetime.tgt(ccache, principal)

        return CcacheLifetime.evidence(ccache, principal)

    @staticmethod
    def _unlink(path: str) -> None:
        try:
            os.unlink(path)
        except FileNotFoundError:
            return

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
            # перечень билетов в причине: по нему видно, есть ли рядом evidence
            arrived = ", ".join(TicketCapture.tickets(ccache))
            return (
                "a forwarded TGT arrived while constrained delegation is "
                f"configured (tickets: {arrived})"
            )

        if evidence == 0:
            return "no evidence ticket (acceptor credentials without a service TGT?)"

        return ""

    @staticmethod
    def tickets(ccache: str) -> list[str]:
        """Имена сервисов в ccache: диагностика того, что прислал клиент."""
        try:
            context = krb5.init_context()
            cache = krb5.cc_resolve(context, ccache.encode())
            names = [
                krb5.unparse_name_flags(context, cred.server).decode() for cred in cache
            ]
        except krb5.Krb5Error as exc:
            return [f"unreadable ccache: {exc}"]

        return [name for name in names if CcacheLifetime.CONFIG_MARK not in name]

    @staticmethod
    def _destroy(ccache: str) -> None:
        try:
            context = krb5.init_context()
            krb5.cc_destroy(context, krb5.cc_resolve(context, ccache.encode()))
        except krb5.Krb5Error:
            return
