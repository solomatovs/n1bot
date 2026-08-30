"""Запечатывание билета входа для JWT: только хост, гостю песочницы не нужно.

Ошибки:
TicketSealError — запечатанный билет не читается: чужой ключ, порча или не тот формат.
"""

from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass
from typing import ClassVar

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from pydantic import ValidationError

from boba.connections.credentials import SignInCredentials
from boba.connections.kerberos import DelegationMode, SignInTicket, TicketSealError
from boba.krb.credentials import DelegatedCredentials

__all__ = ["SsoTickets", "TicketSealer"]


class TicketSealer:
    """Билет под шифром для JWT: ключ выводится из секрета приложения."""

    INFO: ClassVar[bytes] = b"boba-sso-ticket"
    KEY_BYTES: ClassVar[int] = 32
    ENCODING: ClassVar[str] = "utf-8"

    def __init__(self, secret: str) -> None:
        derived = HKDF(
            algorithm=hashes.SHA256(),
            length=self.KEY_BYTES,
            salt=None,
            info=self.INFO,
        ).derive(secret.encode(self.ENCODING))
        self._fernet = Fernet(base64.urlsafe_b64encode(derived))

    def seal(self, ticket: SignInTicket) -> str:
        payload = {
            "principal": ticket.principal,
            "mode": ticket.mode.value,
            "expires_at": ticket.expires_at,
            "ccache": base64.b64encode(ticket.ccache).decode(self.ENCODING),
        }
        plain = json.dumps(payload).encode(self.ENCODING)

        return self._fernet.encrypt(plain).decode(self.ENCODING)

    def open(self, sealed: str) -> SignInTicket:
        try:
            plain = self._fernet.decrypt(sealed.encode(self.ENCODING))
        except InvalidToken as exc:
            msg = "sealed sign-in ticket does not open: wrong key or damaged"
            raise TicketSealError(msg) from exc

        try:
            payload = json.loads(plain.decode(self.ENCODING))
            ccache = base64.b64decode(payload["ccache"], validate=True)
            return SignInTicket(
                principal=payload["principal"],
                mode=DelegationMode(payload["mode"]),
                ccache=ccache,
                expires_at=payload["expires_at"],
            )
        except (
            ValueError,
            KeyError,
            TypeError,
            binascii.Error,
            ValidationError,
        ) as exc:
            msg = f"sealed sign-in ticket is malformed: {exc}"
            raise TicketSealError(msg) from exc


@dataclass(frozen=True)
class SsoTickets(SignInCredentials):
    """Как приложение печатает и открывает билеты входа и превращает их в креды."""

    sealer: TicketSealer
    krb5_config: str

    def seal(self, ticket: SignInTicket) -> str:
        return self.sealer.seal(ticket)

    def open(self, sealed: str) -> SignInTicket:
        return self.sealer.open(sealed)

    def credentials_of(self, ticket: SignInTicket) -> DelegatedCredentials:
        return DelegatedCredentials(ticket, self.krb5_config)
