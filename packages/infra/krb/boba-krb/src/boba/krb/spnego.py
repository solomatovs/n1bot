"""Клиентский SPNEGO: заголовок Authorization: Negotiate по кредам окружения.

Токен строится по KRB5CCNAME/KRB5_CONFIG процесса, поэтому вызывается
внутри KerberosCredentials.applied(); на каждый HTTP-запрос — новый токен,
повторный AP-REQ серверы считают replay.

Ошибки:
KerberosError — GSSAPI не выдал токен к сервису.
"""

from __future__ import annotations

import base64
from typing import ClassVar

import gssapi
from gssapi.raw.misc import GSSError

from boba.krb.errors import GssErrors, KerberosError

__all__ = ["SpnegoNegotiate"]


class SpnegoNegotiate:
    """Значение заголовка Authorization для сервиса service@host."""

    MECH: ClassVar[gssapi.OID] = gssapi.OID.from_int_seq("1.3.6.1.5.5.2")
    HEADER: ClassVar[str] = "Authorization"
    SCHEME: ClassVar[str] = "Negotiate"

    @classmethod
    def header(cls, service: str) -> str:
        try:
            name = gssapi.Name(service, gssapi.NameType.hostbased_service)
            context = gssapi.SecurityContext(name=name, mech=cls.MECH, usage="initiate")
            token = context.step()
        except GSSError as exc:
            raise GssErrors.of(exc, f"spnego init for {service}") from exc

        if not token:
            msg = f"spnego init for {service} produced no token"
            raise KerberosError(msg)

        return f"{cls.SCHEME} {base64.b64encode(token).decode('ascii')}"
