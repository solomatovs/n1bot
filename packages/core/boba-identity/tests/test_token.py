"""Правило продления сессии на claims токена: чистая логика без JWT."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from boba.identity.session import Login
from boba.identity.token import RenewVerdict, SessionClaims, SessionRenewal


class TestSessionRenewal:
    """Правило продления: порог сигнала, grace после exp и потолок сессии."""

    @staticmethod
    def _claims(exp: int, iat: int, since: int) -> SessionClaims:
        return SessionClaims(identifier=Login("alice"), exp=exp, iat=iat, since=since)

    def test_signal_goes_when_the_token_is_about_to_expire(self) -> None:
        renewal = SessionRenewal.of(ttl_sec=3600, max_sec=86400)
        now = 10_000
        soon = self._claims(exp=now + 100, iat=now - 3500, since=now - 3500)
        fresh = self._claims(exp=now + 3000, iat=now - 600, since=now - 600)

        assert renewal.should_refresh(soon, now)
        assert not renewal.should_refresh(fresh, now)

    def test_verdicts(self) -> None:
        renewal = SessionRenewal.of(ttl_sec=3600, max_sec=86400)
        now = 100_000

        alive = self._claims(exp=now + 10, iat=now - 3590, since=now - 3590)
        assert renewal.verdict(alive, now) is RenewVerdict.RENEWABLE

        within_grace = self._claims(exp=now - 100, iat=now - 3700, since=now - 3700)
        assert renewal.verdict(within_grace, now) is RenewVerdict.RENEWABLE

        beyond_grace = self._claims(exp=now - 1000, iat=now - 4600, since=now - 4600)
        assert renewal.verdict(beyond_grace, now) is RenewVerdict.EXPIRED

        old_session = self._claims(exp=now + 10, iat=now - 3590, since=now - 90_000)
        assert renewal.verdict(old_session, now) is RenewVerdict.EXHAUSTED

    def test_token_without_since_counts_from_iat(self) -> None:
        renewal = SessionRenewal.of(ttl_sec=3600, max_sec=7200)
        now = 100_000
        peer = self._claims(exp=now + 10, iat=now - 8000, since=0)

        assert peer.started_at() == now - 8000
        assert renewal.verdict(peer, now) is RenewVerdict.EXHAUSTED

    def test_renewed_claims_keep_the_session_start(self) -> None:
        claims = self._claims(exp=5000, iat=1400, since=1000)

        renewed = claims.renewed(issued_at=6000, ttl_sec=3600)

        assert (renewed.exp, renewed.iat, renewed.since) == (9600, 6000, 1000)
        assert renewed.identifier == claims.identifier


class TestClaimsLogin:
    """Identifier claims — канон Login: чужой токен по секрету несёт любой регистр."""

    def test_identifier_is_canonical_and_display_keeps_the_spelling(self) -> None:
        claims = SessionClaims.model_validate(
            {"identifier": " Ivanov.II ", "exp": 10, "iat": 5}
        )

        assert claims.identifier == "ivanov.ii"
        assert claims.display_name == "Ivanov.II"

    def test_given_display_name_wins(self) -> None:
        claims = SessionClaims.model_validate(
            {"identifier": "Ivanov.II", "display_name": "Иван Иванов", "exp": 10}
        )

        assert claims.identifier == "ivanov.ii"
        assert claims.display_name == "Иван Иванов"

    def test_blank_identifier_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            SessionClaims.model_validate({"identifier": "   ", "exp": 10})

    def test_rendered_claims_carry_the_canonical_login(self) -> None:
        claims = SessionClaims(identifier=Login("Petrov.PP"), exp=10)

        assert claims.render()["identifier"] == "petrov.pp"
