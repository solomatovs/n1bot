"""JwtTokens: выпуск и чтение токена входа, причины отказа."""

from __future__ import annotations

import time

import jwt
import pytest

from boba.auth import JwtTokens
from boba.identity.signin import SignedIn
from boba.identity.token import ClaimKey, TokenRejectedError, TokenRejection

SECRET = "stand-secret"


def _encode(secret: str, identifier: str, ttl_sec: int) -> str:
    claims = {"identifier": identifier, "exp": int(time.time()) + ttl_sec}

    return jwt.encode(claims, secret, algorithm="HS256")


class TestJwtTokens:
    def test_issued_token_reads_back(self) -> None:
        tokens = JwtTokens(SECRET, 60)
        signed = SignedIn(
            identifier="alice", display_name="Alice", metadata={"roles": ["DEV"]}
        )

        claims = tokens.read(tokens.issue(signed))

        assert claims.signed() == signed
        assert claims.exp - claims.iat == 60

    def test_claims_match_the_peer_layout(self) -> None:
        signed = SignedIn(identifier="alice", display_name="Alice", metadata={})
        token = JwtTokens(SECRET, 60).issue(signed)

        raw = jwt.decode(token, SECRET, algorithms=["HS256"])

        assert set(raw) == {key.value for key in ClaimKey}

    @pytest.mark.parametrize(
        ("token", "reason"),
        [
            ("", TokenRejection.MALFORMED),
            ("not-a-jwt", TokenRejection.MALFORMED),
            (_encode("other", "a", 60), TokenRejection.SIGNATURE),
            (_encode(SECRET, "a", -5), TokenRejection.EXPIRED),
            (_encode(SECRET, "", 60), TokenRejection.MALFORMED),
        ],
        ids=["empty", "garbage", "foreign-secret", "expired", "no-identifier"],
    )
    def test_rejection_reasons(self, token: str, reason: TokenRejection) -> None:
        with pytest.raises(TokenRejectedError) as caught:
            JwtTokens(SECRET, 60).read(token)

        assert caught.value.reason is reason

    def test_empty_secret_is_a_build_error(self) -> None:
        with pytest.raises(ValueError, match="secret"):
            JwtTokens("", 60)
