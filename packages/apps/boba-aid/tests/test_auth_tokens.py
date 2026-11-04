"""The AID owns its versioned browser JWT and delegated-ticket claims."""

from __future__ import annotations

from uuid import UUID

import jwt
import pytest
from aid_stand import StandTokens
from pydantic import ValidationError

from boba.aid.auth.claims import TokenRejectedError, TokenRejection
from boba.identity.authentication import RenewalKind
from boba.identity.context import DelegatedTicket
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
        "corporate:alice",
        UUID(int=7),
        roles={"read"},
        profiles={"general"},
        principal="alice@EXAMPLE.TEST",
        sealed_ticket="sealed-ticket",
    )

    claims = _tokens().read(StandTokens.issue(session))

    assert claims.identity == UserIdentity.model_validate("corporate:alice")
    assert claims.user_id == UUID(int=7)
    assert claims.access.roles == {"read"}
    assert claims.access.profiles == {"general"}
    assert claims.renewal is RenewalKind.KERBEROS
    assert claims.ticket() == DelegatedTicket(
        principal="alice@EXAMPLE.TEST", sealed="sealed-ticket"
    )


def test_token_contains_the_declared_v2_wire_contract() -> None:
    token = StandTokens.issue(StandTokens.session("corporate:alice", UUID(int=7)))
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
    assert raw["identity"] == {"realm": "corporate", "login": "alice"}
    assert raw["credential"] == {"kind": "none", "code": "not_delegated"}
    assert raw["renewal"] == "jwt"
    assert "metadata" not in raw


def test_modified_signature_is_refused() -> None:
    token = StandTokens.issue(StandTokens.session("corporate:alice", UUID(int=7)))
    header, payload, signature = token.split(".")
    changed = ("a" if signature[0] != "a" else "b") + signature[1:]
    forged = f"{header}.{payload}.{changed}"

    with pytest.raises(TokenRejectedError) as caught:
        _tokens().read(forged)

    assert caught.value.reason is TokenRejection.SIGNATURE


def test_claims_reject_an_incomplete_or_untrusted_identity() -> None:
    with pytest.raises((TokenRejectedError, ValidationError)):
        StandTokens.session("alice", UUID(int=7))
