"""Билет входа как значение: срок, запечатывание и его отказы."""

from __future__ import annotations

import time

import pytest

from boba.connections.kerberos import DelegationMode, SignInTicket, TicketSealError
from boba.krb.seal import TicketSealer

SECRET = "stand-secret"


def _ticket(expires_in: int) -> SignInTicket:
    return SignInTicket(
        principal="reader@EXAMPLE.COM",
        mode=DelegationMode.CONSTRAINED,
        ccache=b"\x05\x04ccache-bytes",
        expires_at=int(time.time()) + expires_in,
    )


class TestSignInTicket:
    def test_lifetime_counts_down_to_zero(self) -> None:
        if _ticket(600).lifetime() <= 0:
            raise AssertionError("fresh ticket must have a lifetime")
        if _ticket(-1).lifetime() != 0:
            raise AssertionError("expired ticket must report zero")


class TestTicketSealer:
    def test_round_trip_keeps_every_field(self) -> None:
        ticket = _ticket(600)
        sealed = TicketSealer(SECRET).seal(ticket)
        if "ccache-bytes" in sealed:
            raise AssertionError("sealed ticket must not carry the ccache in the open")

        reopened = TicketSealer(SECRET).open(sealed)
        if reopened != ticket:
            raise AssertionError((reopened, ticket))

    def test_other_secret_does_not_open(self) -> None:
        sealed = TicketSealer(SECRET).seal(_ticket(600))
        with pytest.raises(TicketSealError, match="wrong key"):
            TicketSealer("another-secret").open(sealed)

    def test_damaged_token_does_not_open(self) -> None:
        sealed = TicketSealer(SECRET).seal(_ticket(600))
        with pytest.raises(TicketSealError):
            TicketSealer(SECRET).open(sealed[:-8] + "AAAAAAAA")

    def test_garbage_does_not_open(self) -> None:
        with pytest.raises(TicketSealError):
            TicketSealer(SECRET).open("not-a-token")
