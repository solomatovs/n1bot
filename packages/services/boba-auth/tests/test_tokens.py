"""JwtTokens: выпуск и чтение токена входа, причины отказа."""

from __future__ import annotations

import secrets
import time
from collections.abc import Callable

import jwt
import pytest

from boba.auth import JwtTokens
from boba.identity.principal import UserIdentity
from boba.identity.session import Login
from boba.identity.signin import SignedIn, SignInMetadata
from boba.identity.token import ClaimKey, TokenRejectedError, TokenRejection

SECRET = secrets.token_urlsafe(32)


def _encode(secret: str, identifier: str, ttl_sec: int) -> str:
    claims = {"identifier": identifier, "exp": int(time.time()) + ttl_sec}

    return jwt.encode(claims, secret, algorithm="HS256")


class TestJwtTokens:
    def test_issued_token_reads_back(self) -> None:
        tokens = JwtTokens(SECRET, 60, "stand-generation")
        signed = SignedIn(
            identifier=Login("corporate:alice"),
            display_name="Alice",
            sign_in=SignInMetadata(
                identity=UserIdentity(realm="corporate", login=Login("alice")),
                roles=frozenset({"DEV"}),
            ),
        )

        claims = tokens.read(tokens.issue(signed))

        assert claims.signed() == tokens.stamp(signed)
        assert claims.sign_in().generation == "stand-generation"
        assert claims.exp - claims.iat == 60

    def test_token_of_another_generation_is_refused(self) -> None:
        signed = SignedIn(
            identifier=Login("corporate:alice"),
            display_name="Alice",
            sign_in=SignInMetadata(
                identity=UserIdentity(realm="corporate", login=Login("alice"))
            ),
        )
        token = JwtTokens(SECRET, 60, "generation-a").issue(signed)

        with pytest.raises(TokenRejectedError) as caught:
            JwtTokens(SECRET, 60, "generation-b").read(token)

        assert caught.value.reason is TokenRejection.GENERATION

    def test_chainlit_token_without_generation_is_refused(self) -> None:
        token = _encode(SECRET, "alice", 60)

        with pytest.raises(TokenRejectedError) as caught:
            JwtTokens(SECRET, 60, "stand-generation").read(token)

        assert caught.value.reason is TokenRejection.GENERATION

    def test_claims_match_the_peer_layout(self) -> None:
        signed = SignedIn(
            identifier=Login("corporate:alice"),
            display_name="Alice",
            sign_in=SignInMetadata(
                identity=UserIdentity(realm="corporate", login=Login("alice"))
            ),
        )
        token = JwtTokens(SECRET, 60, "stand-generation").issue(signed)

        raw = jwt.decode(token, SECRET, algorithms=["HS256"])

        assert set(raw) == {key.value for key in ClaimKey}

    @pytest.mark.parametrize(
        ("token", "reason"),
        [
            (lambda: "", TokenRejection.MALFORMED),
            (lambda: "not-a-jwt", TokenRejection.MALFORMED),
            (lambda: _encode("other", "a", 60), TokenRejection.SIGNATURE),
            (lambda: _encode(SECRET, "a", -5), TokenRejection.EXPIRED),
            (lambda: _encode(SECRET, "", 60), TokenRejection.MALFORMED),
        ],
        ids=["empty", "garbage", "foreign-secret", "expired", "no-identifier"],
    )
    def test_rejection_reasons(
        self, token: Callable[[], str], reason: TokenRejection
    ) -> None:
        """Токен собирается в самом тесте: срок из параметра истёк бы за прогон."""
        with pytest.raises(TokenRejectedError) as caught:
            JwtTokens(SECRET, 60, "stand-generation").read(token())

        assert caught.value.reason is reason

    def test_empty_secret_is_a_build_error(self) -> None:
        with pytest.raises(ValueError, match="secret"):
            JwtTokens("", 60, "stand-generation")


@pytest.mark.parametrize("identifier", ["alice", "corporate:alice"])
def test_current_generation_without_verified_identity_is_refused(
    identifier: str,
) -> None:
    token = jwt.encode(
        {
            "identifier": identifier,
            "metadata": {"generation": "stand-generation"},
            "exp": int(time.time()) + 60,
        },
        SECRET,
        algorithm="HS256",
    )
    with pytest.raises(TokenRejectedError) as caught:
        JwtTokens(SECRET, 60, "stand-generation").read(token)
    assert caught.value.reason is TokenRejection.IDENTITY
