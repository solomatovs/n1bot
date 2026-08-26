"""Выпуск ccache с одним сервисным билетом: креды тела на один вызов.

Источник — любые креды приложения (TGT из keytab либо делегированный
пользователем). У источника запрашивается билет к SPN назначения, и только
он копируется в свежий FILE-ccache; байты этого файла уезжают в песочницу.
TGT источника остаётся у приложения.

Ошибки:
KerberosError — KDC не выдал билет к назначению или ccache не собрался.
CredentialsExpiredError — креды источника истекли либо выданному билету
    осталось меньше min_lifetime.
KeytabError — keytab источника непригоден.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import tempfile
import time
from collections.abc import Iterator
from typing import ClassVar

import krb5
from gssapi import Credentials, Name, NameType, SecurityContext
from gssapi.raw.misc import GSSError

from boba.connections.kerberos import TicketAuth
from boba.krb.credentials import KerberosCredentials
from boba.krb.errors import CredentialsExpiredError, GssErrors, KerberosError
from boba.toolkit.timing import Elapsed

__all__ = ["ServiceTicketIssuer"]

logger = logging.getLogger(__name__)


class ServiceTicketIssuer:
    """Билет источника к service@host в отдельном ccache, байтами."""

    FILE_TYPE: ClassVar[str] = "FILE"
    TEMP_PREFIX: ClassVar[str] = "boba-ticket-"
    TEMP_NAME: ClassVar[str] = "ccache"

    def __init__(self, min_lifetime: int) -> None:
        self._min_lifetime = min_lifetime

    async def issue_async(
        self, source: KerberosCredentials, service: str
    ) -> TicketAuth:
        """issue() без блокировки event loop: поход в KDC уходит в поток."""
        return await asyncio.to_thread(self.issue, source, service)

    def issue(self, source: KerberosCredentials, service: str) -> TicketAuth:
        elapsed = Elapsed()

        with source.applied():
            self._fetch(source.ccache, service)
            blob = self._export(source.ccache, source.principal, service)

        logger.info(
            "kerberos: service ticket %s -> %s issued in %dms",
            source.principal,
            service,
            elapsed.ms(),
        )

        return TicketAuth.of_bytes(
            principal=source.principal,
            service=service,
            blob=blob,
            min_lifetime=self._min_lifetime,
        )

    @classmethod
    def _fetch(cls, ccache: str, service: str) -> None:
        """Билет к service появляется в ccache источника после initiate.

        Цель задаётся принципалом с realm: по S4U2Proxy krb5 кладёт билет в
        ccache под запрошенным именем, а libpq/clickhouse ищут его с realm.
        """
        principal = cls.principal_of(service)
        try:
            creds = Credentials(usage="initiate", store={b"ccache": ccache.encode()})
            target = Name(principal, NameType.kerberos_principal)
            context = SecurityContext(name=target, creds=creds, usage="initiate")
            context.step()
        except GSSError as exc:
            raise GssErrors.of(exc, f"ticket to {principal}") from exc

    @classmethod
    def principal_of(cls, service: str) -> str:
        """service@host -> service/host@REALM; realm — из krb5.conf."""
        name, sep, host = service.partition("@")
        if not sep or not host:
            msg = f"service {service!r} is not service@host"
            raise KerberosError(msg)

        try:
            context = krb5.init_context()
            parsed = krb5.parse_name_flags(context, f"{name}/{host}".encode())
            return krb5.unparse_name_flags(context, parsed).decode()
        except krb5.Krb5Error as exc:
            msg = f"service {service!r}: principal name is not resolvable"
            raise KerberosError(f"{msg}: {exc}") from exc

    def _export(self, ccache: str, principal: str, service: str) -> bytes:
        """Копия одного билета в свежем FILE-ccache; каталог живёт миг.

        Клиент билета обязан совпадать с принципалом источника: ccache под
        чужой меткой билет за другого не выпускает.
        """
        context = krb5.init_context()
        source = krb5.cc_resolve(context, ccache.encode())

        found = self._service_cred(context, source, service)

        client = krb5.unparse_name_flags(context, found.client).decode()
        if client != principal:
            msg = f"ticket client {client!r} is not the source principal {principal!r}"
            raise KerberosError(msg)

        remaining = found.times.endtime - int(time.time())
        if remaining < self._min_lifetime:
            msg = (
                f"ticket {principal} -> {service} has {remaining}s left, "
                f"less than {self._min_lifetime}s: sign in again"
            )
            raise CredentialsExpiredError(msg)

        directory = tempfile.mkdtemp(prefix=self.TEMP_PREFIX)
        try:
            path = os.path.join(directory, self.TEMP_NAME)
            target = krb5.cc_resolve(context, f"{self.FILE_TYPE}:{path}".encode())
            try:
                krb5.cc_initialize(context, target, found.client)
                krb5.cc_store_cred(context, target, found)
            except krb5.Krb5Error as exc:
                msg = f"ccache with ticket to {service} was not written"
                raise KerberosError(f"{msg}: {exc}") from exc

            with open(path, "rb") as handle:
                return handle.read()
        finally:
            shutil.rmtree(directory, ignore_errors=True)

    @classmethod
    def _service_cred(
        cls, context: krb5.Context, cache: krb5.CCache, service: str
    ) -> krb5.Creds:
        """Самый свежий билет к service: FILE-ccache хранит и устаревшие копии."""
        try:
            candidates = list(cls._matching(context, cache, service))
        except krb5.Krb5Error as exc:
            msg = f"ccache {cache.name!r} is not readable"
            raise KerberosError(f"{msg}: {exc}") from exc

        if not candidates:
            msg = f"ccache holds no ticket to {service} after initiate"
            raise KerberosError(msg)

        return max(candidates, key=cls._endtime)

    @classmethod
    def _matching(
        cls, context: krb5.Context, cache: krb5.CCache, service: str
    ) -> Iterator[krb5.Creds]:
        prefix = cls.server_prefix(service)
        for cred in cache:
            server = krb5.unparse_name_flags(context, cred.server).decode()
            if not server.startswith(prefix):
                continue

            yield cred

    @staticmethod
    def _endtime(cred: krb5.Creds) -> int:
        return cred.times.endtime

    @staticmethod
    def server_prefix(service: str) -> str:
        """service@host -> 'service/host@': начало имени сервера в ccache."""
        name, sep, host = service.partition("@")
        if not sep:
            msg = f"service {service!r} is not service@host"
            raise KerberosError(msg)

        if not host:
            msg = f"service {service!r} has no host"
            raise KerberosError(msg)

        return f"{name}/{host}@"
