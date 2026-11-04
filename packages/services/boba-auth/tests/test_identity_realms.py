"""Выбор пространства выполняется общим входом до выдачи идентификатора."""

import time

import pytest
from pydantic import SecretStr

from boba.auth import SignIns
from boba.auth.config import LocalAuthConfig, ProxyAuthConfig
from boba.auth.providers.proxy import ProxySignature
from boba.identity.errors import AuthenticationError, AuthorizationError
from boba.identity.principal import IdentityRule
from boba.identity.session import Login
from boba.identity.signin import ProxyRequest
from boba.stand.signin import SignInStand

pytestmark = pytest.mark.anyio


def sign_ins(*realms: str) -> SignIns:
    configs = []
    for realm in realms:
        configs.append(
            LocalAuthConfig(
                identity=IdentityRule(realm=realm),
                users={"alice": realm},
                require_roles=False,
            )
        )

    return SignIns(SignInStand.backends().password(configs), {}, {})


async def test_one_realm_does_not_require_an_extra_login_field() -> None:
    auth = sign_ins("office")
    signed = await auth.by_password("alice", "office")

    assert signed.identifier == "office:alice"
    assert auth.providers().password_realms == ("office",)


async def test_multiple_realms_require_an_explicit_choice() -> None:
    auth = sign_ins("office", "partner")
    with pytest.raises(AuthorizationError, match="select an identity realm"):
        await auth.by_password("alice", "office")

    office = await auth.by_password("alice", "office", "office")
    partner = await auth.by_password("alice", "partner", "partner")

    assert office.identifier == "office:alice"
    assert partner.identifier == "partner:alice"


async def test_selected_realm_does_not_fall_through_to_another_backend() -> None:
    auth = sign_ins("office", "partner")
    with pytest.raises(AuthenticationError):
        await auth.by_password("alice", "partner", "office")

    with pytest.raises(AuthorizationError, match="not configured"):
        await auth.by_password("alice", "office", "unknown")


async def test_backend_alias_is_resolved_before_the_identifier_is_issued() -> None:
    config = LocalAuthConfig(
        identity=IdentityRule(
            realm="office", aliases={Login("a.smith"): Login("alice")}
        ),
        users={"a.smith": "pass"},
        require_roles=False,
    )
    auth = SignIns(SignInStand.backends().password([config]), {}, {})
    signed = await auth.by_password("a.smith", "pass")

    assert signed.identifier == "office:alice"


async def test_proxy_selects_the_configured_backend_before_verifying_credentials() -> (
    None
):
    configs = []
    for realm in ("office", "partner"):
        configs.append(
            ProxyAuthConfig(
                identity=IdentityRule(realm=realm),
                secret=SecretStr(f"{realm}-signing-key"),
                require_roles=False,
            )
        )
    auth = SignInStand.backends().build(configs, "session-key")

    def request(secret: str) -> ProxyRequest:
        unsigned = ProxyRequest(login="alice", timestamp=str(int(time.time())))
        return unsigned.model_copy(
            update={"signature": ProxySignature(secret).sign(unsigned)}
        )

    office = await auth.by_proxy(request("office-signing-key"), "office")
    partner = await auth.by_proxy(request("partner-signing-key"), "partner")
    assert office.identifier == "office:alice"
    assert partner.identifier == "partner:alice"
    with pytest.raises(AuthenticationError, match="signature"):
        await auth.by_proxy(request("office-signing-key"), "partner")

    with pytest.raises(AuthorizationError, match="select an identity realm"):
        await auth.by_proxy(request("office-signing-key"))
