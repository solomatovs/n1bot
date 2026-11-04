"""Подписанное утверждение связывает идентификатор, пространство и издателя."""

import secrets

import jwt
import pytest
from pydantic import SecretStr

from boba.auth.config import ProxyAuthConfig
from boba.oauth.assertions import AssertionRejectedError, ProxyAssertions
from boba.oauth.server.assertions import ProxyAssertionVerifier
from boba.identity.principal import IdentityRule


def test_verifier_uses_the_key_of_the_asserted_realm() -> None:
    office = ProxyAuthConfig(
        identity=IdentityRule(realm="office"), secret=SecretStr("office-key")
    )
    partner = ProxyAuthConfig(
        identity=IdentityRule(realm="partner"), secret=SecretStr("partner-key")
    )
    verifier = ProxyAssertionVerifier((office, partner))
    token = ProxyAssertions("partner-key", 0).issue(
        "client", "partner:alice", "read", "https://service", "https://service/mcp"
    )

    accepted = verifier.read(token, "client", "https://service")
    assert accepted.sub == "partner:alice"
    wrong_key = ProxyAssertions("office-key", 0).issue(
        "client", "partner:alice", "read", "https://service", "https://service/mcp"
    )
    with pytest.raises(AssertionRejectedError):
        verifier.read(wrong_key, "client", "https://service")


@pytest.mark.parametrize(
    ("field", "value"), [("iss", "another-client"), ("sub", "partner:alice")]
)
def test_matching_signature_does_not_allow_an_inconsistent_identity(
    field: str, value: str
) -> None:
    secret = secrets.token_urlsafe(32)
    signer = ProxyAssertions(secret, 0)
    token = signer.issue(
        "client", "office:alice", "read", "https://service", "https://service/mcp"
    )
    claims = jwt.decode(token, secret, algorithms=["HS256"], audience="https://service")
    claims[field] = value
    changed = jwt.encode(
        claims,
        secret,
        algorithm="HS256",
        headers=jwt.get_unverified_header(token),
    )

    with pytest.raises(AssertionRejectedError):
        signer.read(changed, "client", "https://service")


def test_key_id_cannot_relabel_a_verified_identity() -> None:
    secret = secrets.token_urlsafe(32)
    signer = ProxyAssertions(secret, 0)
    token = signer.issue(
        "client", "office:alice", "read", "https://service", "https://service/mcp"
    )
    claims = jwt.decode(token, secret, algorithms=["HS256"], audience="https://service")
    changed = jwt.encode(
        claims,
        secret,
        algorithm="HS256",
        headers={"typ": "oauth-id-jag+jwt", "kid": "partner"},
    )

    with pytest.raises(AssertionRejectedError, match="key id"):
        signer.read(changed, "client", "https://service")
