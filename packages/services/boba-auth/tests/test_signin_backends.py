"""Все бэкенды выдают канонический логин без выбора источника пользователем."""

import time

import pytest
from pydantic import SecretStr

from boba.auth import SignIns
from boba.auth.config import LocalAuthConfig, ProxyAuthConfig
from boba.auth.providers.proxy import ProxySignature
from boba.identity.errors import AuthenticationError
from boba.identity.principal import IdentityRule
from boba.identity.session import Login
from boba.identity.signin import ProxyRequest
from boba.stand.signin import SignInStand

pytestmark = pytest.mark.anyio


def sign_ins(*passwords: str) -> SignIns:
    configs = [
        LocalAuthConfig(users={"alice": password}, require_roles=False)
        for password in passwords
    ]
    return SignIns(SignInStand.backends().password(configs), None, None)


async def test_password_sign_in_uses_the_canonical_login() -> None:
    auth = sign_ins("password")
    signed = await auth.by_password("alice", "password")
    assert signed.identifier == "alice"
    assert auth.providers().password


async def test_backends_authenticating_the_same_login_return_the_same_identity() -> (
    None
):
    auth = sign_ins("first-password", "second-password")
    first = await auth.by_password("alice", "first-password")
    second = await auth.by_password("alice", "second-password")
    assert first.identity == second.identity
    assert first.identifier == "alice"


async def test_unrecognized_credentials_are_refused_after_all_backends() -> None:
    auth = sign_ins("first-password", "second-password")
    with pytest.raises(AuthenticationError):
        await auth.by_password("alice", "wrong-password")


async def test_backend_alias_is_resolved_before_the_identifier_is_issued() -> None:
    config = LocalAuthConfig(
        identity=IdentityRule(aliases={Login("a.smith"): Login("alice")}),
        users={"a.smith": "pass"},
        require_roles=False,
    )
    auth = SignIns(SignInStand.backends().password([config]), None, None)
    signed = await auth.by_password("a.smith", "pass")
    assert signed.identifier == "alice"


async def test_local_and_proxy_sign_in_return_the_same_identity() -> None:
    proxy = ProxyAuthConfig(secret=SecretStr("proxy-signing-key"), require_roles=False)
    local = LocalAuthConfig(users={"alice": "password"}, require_roles=False)
    auth = SignInStand.backends().build([local, proxy], "session-key")
    unsigned = ProxyRequest(login="alice", timestamp=str(int(time.time())))
    signed = unsigned.model_copy(
        update={"signature": ProxySignature("proxy-signing-key").sign(unsigned)}
    )
    first = await auth.by_password("alice", "password")
    second = await auth.by_proxy(signed)
    assert first.identity == second.identity
    with pytest.raises(AuthenticationError, match="signature"):
        await auth.by_proxy(signed.model_copy(update={"signature": "wrong"}))
