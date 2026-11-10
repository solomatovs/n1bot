"""The AID owns its versioned browser JWT and delegated-ticket claims."""

from __future__ import annotations

import time
from functools import partial
from uuid import UUID

import jwt
import pytest
from aid_stand import StandTokens
from pydantic import ValidationError

from boba.aid.auth.claims import TokenError, TokenRejectedError, TokenRejection
from boba.aid.auth.tokens import JwtTokens
from boba.identity.authentication import RenewalKind
from boba.identity.context import DelegatedTicket
from boba.identity.lifetime import SessionLifetime
from boba.identity.principal import UserIdentity

SECRET = "token-contract-test-secret"


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    """Token serialization is independent of a Chainlit HTTP session."""


def _tokens() -> StandTokens:
    tokens = StandTokens()
    # StandTokens reads Chainlit's configured key when present; use its public
    # issuer settings while tests elsewhere exercise the configured secret.
    return tokens


def test_issued_token_round_trips_identity_access_and_credential() -> None:
    session = StandTokens.session(
        "alice",
        UUID(int=7),
        roles={"read"},
        profiles={"general"},
        principal="alice@EXAMPLE.TEST",
        sealed_ticket="sealed-ticket",
    )

    claims = _tokens().read(StandTokens.issue(session))

    assert claims.identity == UserIdentity.model_validate("alice")
    assert claims.user_id == UUID(int=7)
    assert claims.access.roles == {"read"}
    assert claims.access.profiles == {"general"}
    assert claims.renewal is RenewalKind.KERBEROS
    assert claims.ticket() == DelegatedTicket(
        principal="alice@EXAMPLE.TEST", sealed="sealed-ticket"
    )


def test_token_contains_the_declared_v2_wire_contract() -> None:
    token = StandTokens.issue(StandTokens.session("alice", UUID(int=7)))
    raw = jwt.decode(
        token,
        StandTokens.secret(),
        algorithms=["HS256"],
        issuer=StandTokens.ISSUER,
        audience=StandTokens.ISSUER,
    )

    assert raw["version"] == 2
    assert raw["kind"] == "session"
    assert raw["sub"] == str(UUID(int=7))
    assert raw["identity"] == {"login": "alice"}
    assert raw["credential"] == {"kind": "none", "code": "not_delegated"}
    assert raw["renewal"] == "jwt"
    assert "metadata" not in raw


def test_modified_signature_is_refused() -> None:
    token = StandTokens.issue(StandTokens.session("alice", UUID(int=7)))
    header, payload, signature = token.split(".")
    changed = ("a" if signature[0] != "a" else "b") + signature[1:]
    forged = f"{header}.{payload}.{changed}"

    with pytest.raises(TokenRejectedError) as caught:
        _tokens().read(forged)

    assert caught.value.reason is TokenRejection.SIGNATURE


@pytest.mark.parametrize("login", ["", "   "])
def test_claims_reject_an_empty_identity(login: str) -> None:
    with pytest.raises((TokenRejectedError, ValidationError)):
        StandTokens.session(login, UUID(int=7))


@pytest.fixture
def codec() -> JwtTokens:
    return JwtTokens(
        SECRET,
        SessionLifetime(ttl_sec=3600, max_sec=7200),
        "boundary-test",
        issuer="token-boundary",
        audience="token-boundary",
    )


@pytest.mark.parametrize("stale", [False, True])
def test_unexpected_jwt_decoder_failure_has_token_boundary(
    codec: JwtTokens, stale: bool
) -> None:
    token = codec.issue(StandTokens.session("alice", UUID(int=7)), int(time.time()))
    unreadable_key = JwtTokens(
        "\ud800",
        SessionLifetime(ttl_sec=3600, max_sec=7200),
        "boundary-test",
        issuer="token-boundary",
        audience="token-boundary",
    )
    operation = unreadable_key.read
    if stale:
        operation = partial(unreadable_key.read_stale, grace_sec=60)

    with pytest.raises(TokenError) as failed:
        operation(token)

    assert not isinstance(failed.value, TokenRejectedError)
    assert isinstance(failed.value.__cause__, UnicodeEncodeError)
    assert "token-boundary" in str(failed.value)
    assert token not in str(failed.value)


def test_token_issuance_preserves_model_failure_within_its_contract(
    codec: JwtTokens,
) -> None:
    session = StandTokens.session("alice", UUID(int=7))
    with pytest.raises(TokenError) as failed:
        codec.issue(session, issued_at=0)

    assert isinstance(failed.value.__cause__, ValidationError)
    assert "issuing token failed" in str(failed.value)
