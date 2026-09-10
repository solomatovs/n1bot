"""Вход по доверенному заголовку: подпись, окно времени, адрес клиента и три
источника ролей. Роли из каталога — живым LDAP стенда.
"""

from __future__ import annotations

import time
from typing import Any

import pytest
from omegaconf import DictConfig
from pydantic import SecretStr

from boba.auth.config import (
    HeaderProfilesConfig,
    HeaderRolesConfig,
    LdapRolesConfig,
    LocalRolesConfig,
    ProxyAuthConfig,
    ProxyProfileProviders,
    ProxyRoleProviders,
)
from boba.auth.proxy import ProxySignature
from boba.config import bind
from boba.identity.admission import RoleExcludeConfig, RoleMappingConfig
from boba.identity.errors import AuthenticationError, AuthorizationError
from boba.identity.session import SignInProvider
from boba.identity.signin import ProxyRequest, ProxySignIn
from boba.stand.signin import SignInStand
from boba.stand.site import Stand

STAND = Stand.required()

pytestmark = pytest.mark.anyio

SECRET = "proxy-stand-secret"


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    """Провайдер зовётся напрямую: сессия чата не нужна."""


def _providers(**update: object) -> ProxyRoleProviders:
    base = ProxyRoleProviders(
        local=LocalRolesConfig(
            mapping=RoleMappingConfig(root={"maksimov.ma": ["read"]}),
            exclude=RoleExcludeConfig(root=["banned"]),
        ),
        header=HeaderRolesConfig(name="X-Remote-Roles"),
    )

    return base.model_copy(update=update)


def _config(**update: object) -> ProxyAuthConfig:
    base = ProxyAuthConfig(secret=SecretStr(SECRET), roles=_providers())

    return base.model_copy(update=update)


def _sign_in(config: ProxyAuthConfig) -> ProxySignIn:
    """Вход по конфигу через сборку стенда: тест — точка bootstrap."""
    return SignInStand.assembly().proxy(config)


def _request(
    login: str = "Maksimov.MA",
    roles: str = "",
    client: str = "172.18.0.20",
    timestamp: int | None = None,
    secret: str = SECRET,
) -> ProxyRequest:
    """Подписанный запрос без профилей: набор и выбор — через _with_profiles."""
    if timestamp is None:
        timestamp = int(time.time())

    unsigned = ProxyRequest(
        login=login, timestamp=str(timestamp), roles=roles, client=client
    )

    return _signed(unsigned, secret)


def _with_profiles(
    request: ProxyRequest, profiles: str = "", profile: str = ""
) -> ProxyRequest:
    """Тот же запрос с заголовками профилей и подписью, покрывающей их."""
    unsigned = request.model_copy(
        update={"profiles": profiles, "profile": profile, "signature": ""}
    )

    return _signed(unsigned, SECRET)


def _signed(unsigned: ProxyRequest, secret: str) -> ProxyRequest:
    signature = ProxySignature(secret).sign(unsigned)

    return unsigned.model_copy(update={"signature": signature})


async def test_signed_login_is_canonical_with_table_roles() -> None:
    signed = await _sign_in(_config()).sign_in(_request())

    assert signed.identifier == "maksimov.ma"
    assert signed.display_name == "Maksimov.MA"
    assert signed.sign_in.provider == SignInProvider.PROXY.value
    assert signed.sign_in.roles == frozenset({"read"})


async def test_header_roles_join_table_roles() -> None:
    signed = await _sign_in(_config()).sign_in(_request(roles="wrt, admin"))

    assert signed.sign_in.roles == frozenset({"read", "wrt", "admin"})


async def test_header_roles_are_ignored_when_the_provider_is_not_configured() -> None:
    config = _config(roles=_providers(header=None))
    signed = await _sign_in(config).sign_in(_request(roles="wrt"))

    assert signed.sign_in.roles == frozenset({"read"})


async def test_tampered_roles_break_the_signature() -> None:
    request = _request(roles="read").model_copy(update={"roles": "read,admin"})

    with pytest.raises(AuthenticationError, match="signature does not match"):
        await _sign_in(_config()).sign_in(request)


async def test_foreign_secret_is_refused() -> None:
    with pytest.raises(AuthenticationError, match="signature does not match"):
        await _sign_in(_config()).sign_in(_request(secret="other"))


async def test_stale_timestamp_is_refused() -> None:
    old = int(time.time()) - 600

    with pytest.raises(AuthenticationError, match="max_skew_sec"):
        await _sign_in(_config()).sign_in(_request(timestamp=old))


@pytest.mark.parametrize(
    "request_",
    [
        ProxyRequest(),
        ProxyRequest(login="maksimov.ma"),
        ProxyRequest(login="maksimov.ma", timestamp="1"),
    ],
    ids=["no-login", "no-timestamp", "no-signature"],
)
async def test_missing_headers_are_refused(request_: ProxyRequest) -> None:
    with pytest.raises(AuthenticationError, match="is missing"):
        await _sign_in(_config()).sign_in(request_)


async def test_client_outside_allowed_networks_is_refused() -> None:
    config = _config(allowed_clients=["172.18.0.0/24"])

    with pytest.raises(AuthorizationError, match="outside allowed_clients"):
        await _sign_in(config).sign_in(_request(client="10.0.0.5"))


async def test_client_inside_allowed_networks_passes() -> None:
    config = _config(allowed_clients=["172.18.0.0/24"])
    signed = await _sign_in(config).sign_in(_request(client="172.18.0.20"))

    assert signed.identifier == "maksimov.ma"


async def test_excluded_login_is_refused_even_with_header_roles() -> None:
    with pytest.raises(AuthorizationError, match="exclusion rule"):
        await _sign_in(_config()).sign_in(_request(login="banned", roles="admin"))


async def test_no_roles_from_any_source_is_refused() -> None:
    with pytest.raises(AuthorizationError, match="no role came"):
        await _sign_in(_config()).sign_in(_request(login="stranger"))


async def test_require_roles_off_admits_without_roles() -> None:
    config = _config(require_roles=False)
    signed = await _sign_in(config).sign_in(_request(login="stranger"))

    assert signed.sign_in.roles == frozenset()


def test_config_rejects_bad_network() -> None:
    with pytest.raises(ValueError, match="not a CIDR network"):
        ProxyAuthConfig(secret=SecretStr("x"), allowed_clients=["not-an-ip"])


def test_config_rejects_blank_secret() -> None:
    with pytest.raises(ValueError, match="non-empty signing key"):
        ProxyAuthConfig(secret=SecretStr("  "))


@pytest.mark.integration
@pytest.mark.skipif(not STAND.live(), reason="нет keytab/krb5.conf локального AD")
async def test_directory_roles_by_sam_account_name(raw_config: Any) -> None:
    """Роли из каталога: поиск по sAMAccountName под служебным bind'ом."""
    ldap = bind(raw_config, path="auth.kerberos.roles.ldap", model=LdapRolesConfig)
    config = _config(roles=_providers(local=None, ldap=ldap), require_roles=False)
    _, _, reader = STAND.ldap_bind_user.rpartition(Stand.NETBIOS_SEPARATOR)

    signed = await _sign_in(config).sign_in(_request(login=reader))

    assert signed.identifier == reader.lower()
    assert isinstance(signed.sign_in.roles, frozenset)


@pytest.mark.integration
@pytest.mark.skipif(not STAND.live(), reason="нет keytab/krb5.conf локального AD")
async def test_login_unknown_to_the_directory_keeps_other_roles(
    raw_config: DictConfig,
) -> None:
    """Каталог — источник ролей, не личности: неизвестный ему логин входит с
    ролями таблицы и заголовка, а без них отказ по require_roles."""
    ldap = bind(raw_config, path="auth.kerberos.roles.ldap", model=LdapRolesConfig)
    config = _config(roles=_providers(ldap=ldap))
    provider = _sign_in(config)

    signed = await provider.sign_in(_request(login="maksimov.ma", roles="ops"))
    assert signed.sign_in.roles == frozenset({"read", "ops"})

    with pytest.raises(AuthorizationError, match="no role came"):
        await provider.sign_in(_request(login="nobody-here"))


def _with_profile_headers() -> ProxyAuthConfig:
    """Конфиг с провайдером профилей по заголовкам: набор и выбранный."""
    return _config(
        profiles=ProxyProfileProviders(
            header=HeaderProfilesConfig(
                name="X-Remote-Profiles", selected="X-Remote-Profile"
            )
        )
    )


async def test_selected_profile_from_the_header_lands_in_the_sign_in() -> None:
    signed = await _sign_in(_with_profile_headers()).sign_in(
        _with_profiles(
            _request(), profiles=SignInStand.PROFILE, profile=SignInStand.PROFILE
        )
    )

    assert signed.sign_in.profiles == frozenset({SignInStand.PROFILE})
    assert signed.sign_in.profile == SignInStand.PROFILE


async def test_selected_profile_outside_the_grant_is_refused() -> None:
    config = _with_profile_headers()
    stand = SignInStand.assembly(SignInStand.profiles(roles=["nobody"])).proxy(config)

    with pytest.raises(AuthorizationError, match="not among the granted"):
        await stand.sign_in(_with_profiles(_request(), profile=SignInStand.PROFILE))


async def test_unknown_selected_profile_is_refused() -> None:
    with pytest.raises(AuthorizationError, match="not in \\[profiles\\]"):
        await _sign_in(_with_profile_headers()).sign_in(
            _with_profiles(_request(), profile="nope")
        )


async def test_tampered_selected_profile_breaks_the_signature() -> None:
    request = _with_profiles(_request(), profile=SignInStand.PROFILE).model_copy(
        update={"profile": "other"}
    )

    with pytest.raises(AuthenticationError, match="signature does not match"):
        await _sign_in(_with_profile_headers()).sign_in(request)


async def test_sign_in_without_the_selected_header_keeps_no_choice() -> None:
    signed = await _sign_in(_with_profile_headers()).sign_in(_request())

    assert signed.sign_in.profile == ""
