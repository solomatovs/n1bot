"""Подписанное утверждение связывает логин и издателя."""

import secrets

import jwt
import pytest
from pydantic import SecretStr

from boba.auth.config import ProxyAuthConfig
from boba.oauth.assertions import AssertionRejectedError, ProxyAssertions
from boba.oauth.server.assertions import ProxyAssertionVerifier


def test_verifier_uses_the_configured_proxy_key() -> None:
    config = ProxyAuthConfig(secret=SecretStr("proxy-key"))
    verifier = ProxyAssertionVerifier((config,))
    token = ProxyAssertions("proxy-key", 0).issue(
        "client", "alice", "read", "https://service", "https://service/mcp"
    )
    accepted = verifier.read(token, "client", "https://service")
    assert accepted.sub == "alice"
    assert accepted.identity.identifier == "alice"
    wrong_key = ProxyAssertions("wrong-key", 0).issue(
        "client", "alice", "read", "https://service", "https://service/mcp"
    )
    with pytest.raises(AssertionRejectedError):
        verifier.read(wrong_key, "client", "https://service")


@pytest.mark.parametrize(
    ("field", "value"), [("iss", "another-client"), ("sub", "bob")]
)
def test_matching_signature_does_not_allow_an_inconsistent_identity(
    field: str, value: str
) -> None:
    secret = secrets.token_urlsafe(32)
    signer = ProxyAssertions(secret, 0)
    token = signer.issue(
        "client", "alice", "read", "https://service", "https://service/mcp"
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
