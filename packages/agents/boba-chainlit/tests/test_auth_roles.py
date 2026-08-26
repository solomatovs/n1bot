"""Роли и исключения авторизации: чистые мапперы без KDC/LDAP.

Интеграционный путь (реальный SPNEGO/AD) в тестовом окружении недоступен,
поэтому проверяется чистая логика маппинга SID/ролей и решений LocalAuth.
"""

from __future__ import annotations

import pytest
from conftest import FakeSecret

from boba.chainlit.auth.config import (
    KerberosRolesInLdapConfig,
    KerberosRolesInLdapMappingConfig,
    LocalAuthConfig,
)
from boba.chainlit.auth.kerberos import (
    KerberosRolesInLdapProvider,
    SidExcludeUserProvider,
    SidUserRolesProvider,
)
from boba.chainlit.auth.local import LocalAuth
from boba.identity.directory import ADUserEntry
from boba.identity.errors import AuthorizationError
from boba.identity.roles import RoleExcludeConfig, RoleMappingConfig
from boba.identity.session import UserLogin, UserMetadataField
from boba.identity.sso import SidsHeader

pytestmark = pytest.mark.anyio


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    pass


def test_sids_header_roundtrip() -> None:
    sids = ["S-1-5-21-1", "S-1-5-21-2"]
    raw = SidsHeader.render(sids)
    if SidsHeader.parse(raw) != sids:
        raise AssertionError("SidsHeader.parse(raw) == sids")


def test_sids_header_parse_skips_empty_parts() -> None:
    if SidsHeader.parse("S-1-5-1,,S-1-5-2,") != ["S-1-5-1", "S-1-5-2"]:
        raise AssertionError('SidsHeader.parse("S-1-5-1,,S-1-5-2,") == ["S-1-5-1", "S…')
    if SidsHeader.parse("") != []:
        raise AssertionError('SidsHeader.parse("") == []')


def test_sid_roles_maps_each_group() -> None:
    mapping = RoleMappingConfig(
        {"S-1-5-21-1": ["admin"], "S-1-5-21-2": ["dev"]},
    )
    provider = SidUserRolesProvider(mapping)
    roles = list(provider.roles_of(["S-1-5-21-1", "S-1-5-21-2", "S-1-5-21-3"]))
    if sorted(roles) != ["admin", "dev"]:
        raise AssertionError('sorted(roles) == ["admin", "dev"]')


def test_sid_exclude_flags_matching_group() -> None:
    mapping = RoleExcludeConfig(["S-1-5-21-9"])
    provider = SidExcludeUserProvider(mapping)
    if not (any(provider.exclude_of(["S-1-5-21-9"]))):
        raise AssertionError('any(provider.exclude_of(["S-1-5-21-9"]))')
    if any(provider.exclude_of(["S-1-5-21-1"])):
        raise AssertionError('not any(provider.exclude_of(["S-1-5-21-1"]))')


async def test_local_auth_allows_user_with_roles() -> None:
    config = LocalAuthConfig(
        users={"alice": "pw"},
        roles=RoleMappingConfig({"alice": ["admin"]}),
    )
    user = await LocalAuth(config).password_auth("alice", "pw")
    if user is None:
        raise AssertionError("user is not None")
    if user.identifier != "alice":
        raise AssertionError('user.identifier == "alice"')
    if user.metadata[UserMetadataField.ROLES] != ["admin"]:
        raise AssertionError('user.metadata[UserMetadataField.ROLES] == ["admin"]')


async def test_local_auth_rejects_wrong_password() -> None:
    config = LocalAuthConfig(users={"alice": "pw"})
    user = await LocalAuth(config).password_auth("alice", "nope")
    if user is not None:
        raise AssertionError("user is None")


async def test_local_auth_rejects_excluded_user() -> None:
    config = LocalAuthConfig(
        users={"alice": "pw"},
        roles_ex=RoleExcludeConfig(["alice"]),
    )
    with pytest.raises(AuthorizationError):
        await LocalAuth(config).password_auth("alice", "pw")


async def test_local_auth_rejects_no_roles_when_required() -> None:
    config = LocalAuthConfig(users={"alice": "pw"})
    with pytest.raises(AuthorizationError):
        await LocalAuth(config).password_auth("alice", "pw")


async def test_local_auth_allows_no_roles_when_not_required() -> None:
    config = LocalAuthConfig(users={"alice": "pw"}, require_roles=False)
    user = await LocalAuth(config).password_auth("alice", "pw")
    if user is None:
        raise AssertionError("user is not None")
    if UserMetadataField.ROLES in user.metadata:
        raise AssertionError("UserMetadataField.ROLES not in user.metadata")


def test_ldap_provider_maps_roles_from_all_sources() -> None:
    mapping = KerberosRolesInLdapMappingConfig(
        samaccountname=RoleMappingConfig({"alice": ["admin"]}),
        member_of=RoleMappingConfig({"CN=Devs,OU=G": ["dev"]}),
        dn=RoleMappingConfig({"CN=alice,OU=U": ["devops"]}),
    )
    provider = KerberosRolesInLdapProvider(
        KerberosRolesInLdapConfig(
            server="ldaps://dc.example.com:636",
            base_dn="DC=example,DC=com",
            bind_dn="cn=svc",
            bind_password=FakeSecret.LDAP_BIND,
            mapping=mapping,
        ),
    )
    user = ADUserEntry(
        dn="CN=alice,OU=U",
        samaccountname="alice",
        member_of=["CN=Devs,OU=G", "CN=Other,OU=G"],
    )
    roles = list(provider.roles_of(user))
    if sorted(roles) != ["admin", "dev", "devops"]:
        raise AssertionError('sorted(roles) == ["admin", "dev", "devops"]')


def test_ldap_provider_excludes_by_any_source() -> None:
    mapping = KerberosRolesInLdapMappingConfig(
        samaccountname_ex=RoleExcludeConfig(["bob"]),
        member_of_ex=RoleExcludeConfig(["CN=Blocked,OU=G"]),
    )
    provider = KerberosRolesInLdapProvider(
        KerberosRolesInLdapConfig(
            server="ldaps://dc.example.com:636",
            base_dn="DC=example,DC=com",
            bind_dn="cn=svc",
            bind_password=FakeSecret.LDAP_BIND,
            mapping=mapping,
        ),
    )

    blocked_by_name = ADUserEntry(
        dn="CN=bob,OU=U",
        samaccountname="bob",
        member_of=[],
    )
    if provider.excluded_of(blocked_by_name) is not True:
        raise AssertionError("provider.excluded_of(blocked_by_name) is True")

    blocked_by_group = ADUserEntry(
        dn="CN=carol,OU=U",
        samaccountname="carol",
        member_of=["CN=Blocked,OU=G"],
    )
    if provider.excluded_of(blocked_by_group) is not True:
        raise AssertionError("provider.excluded_of(blocked_by_group) is True")

    allowed = ADUserEntry(
        dn="CN=carol,OU=U",
        samaccountname="carol",
        member_of=["CN=Other,OU=G"],
    )
    if provider.excluded_of(allowed) is not False:
        raise AssertionError("provider.excluded_of(allowed) is False")


class TestUserLoginCanon:
    """Регистр набранного логина не заводит вторую личность."""

    def test_key_is_lowered_and_display_keeps_the_source(self) -> None:
        login = UserLogin.of("  Maksimov.MA ")

        if login.key != "maksimov.ma":
            raise AssertionError(f"ключ в нижнем регистре, дано {login.key!r}")

        if login.display != "Maksimov.MA":
            raise AssertionError(f"вид как в источнике, дано {login.display!r}")

    def test_different_case_gives_one_key(self) -> None:
        keys = {UserLogin.of(name).key for name in ("MAKSIMOV.MA", "Maksimov.MA")}

        if keys != {"maksimov.ma"}:
            raise AssertionError(f"один ключ на все написания, дано {keys!r}")


class TestLocalAuthIdentifier:
    """LocalAuth: в базу уходит канон логина, в интерфейс — как в конфиге."""

    async def test_identifier_is_the_canonical_login(self) -> None:
        config = LocalAuthConfig(
            users={"Maksimov.MA": "pw"},
            roles=RoleMappingConfig({"Maksimov.MA": ["admin"]}),
        )

        user = await LocalAuth(config).password_auth("Maksimov.MA", "pw")

        if user is None:
            raise AssertionError("user is not None")

        if user.identifier != "maksimov.ma":
            raise AssertionError(f"identifier канонизирован, дано {user.identifier!r}")

        if user.display_name != "Maksimov.MA":
            raise AssertionError(f"display как в конфиге, дано {user.display_name!r}")
