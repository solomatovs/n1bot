"""Запечатывание билета входа для JWT: только хост, гостю песочницы не нужно.

Ошибки:
TicketSealError — запечатанный билет не читается: чужой ключ, порча или не тот формат.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import ClassVar

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from pydantic import ValidationError

from boba.kerberos import (
    SignInCredentials,
    SignInTicket,
    TicketSealError,
)
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
        plain = ticket.model_dump_json().encode(self.ENCODING)

        return self._fernet.encrypt(plain).decode(self.ENCODING)

    def open(self, sealed: str) -> SignInTicket:
        try:
            plain = self._fernet.decrypt(sealed.encode(self.ENCODING))
        except InvalidToken as exc:
            msg = (
                f"opening sealed sign-in ticket ({len(sealed)} chars) failed: "
                "wrong key or damaged token"
            )
            raise TicketSealError(msg) from exc

        try:
            return SignInTicket.model_validate_json(plain)
        except ValidationError as exc:
            msg = (
                f"sealed sign-in ticket opened, but its payload is not a "
                f"SignInTicket: {exc}"
            )
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
