"""Роли и исключения авторизации: чистые мапперы без KDC/LDAP.

Интеграционный путь (реальный SPNEGO/AD) в тестовом окружении недоступен,
поэтому проверяется чистая логика маппинга SID/ролей и решений LocalAuth.
"""

from __future__ import annotations

import pytest

from boba.chainlit.auth.errors import AuthorizationError
from boba.chainlit.auth.kerberos import (
    KerberosRolesInLdapConfig,
    KerberosRolesInLdapMappingConfig,
    KerberosRolesInLdapProvider,
    SidsHeader,
    SidExcludeUserProvider,
    SidUserRolesProvider,
)
from boba.chainlit.auth.ldap import ADUserEntry
from boba.chainlit.auth.local import (
    LocalAuth,
    LocalAuthConfig,
    RoleExcludeConfig,
    RoleMappingConfig,
)
from boba.chainlit.infra.session import UserMetadataField

pytestmark = pytest.mark.anyio


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    pass


def test_sids_header_roundtrip() -> None:
    sids = ["S-1-5-21-1", "S-1-5-21-2"]
    raw = SidsHeader.render(sids)
    assert SidsHeader.parse(raw) == sids


def test_sids_header_parse_skips_empty_parts() -> None:
    assert SidsHeader.parse("S-1-5-1,,S-1-5-2,") == ["S-1-5-1", "S-1-5-2"]
    assert SidsHeader.parse("") == []


def test_sid_roles_maps_each_group() -> None:
    mapping = RoleMappingConfig(
        {"S-1-5-21-1": ["admin"], "S-1-5-21-2": ["dev"]},
    )
    provider = SidUserRolesProvider(mapping)
    roles = list(provider.roles_of(["S-1-5-21-1", "S-1-5-21-2", "S-1-5-21-3"]))
    assert sorted(roles) == ["admin", "dev"]


def test_sid_exclude_flags_matching_group() -> None:
    mapping = RoleExcludeConfig(["S-1-5-21-9"])
    provider = SidExcludeUserProvider(mapping)
    assert any(provider.exclude_of(["S-1-5-21-9"]))
    assert not any(provider.exclude_of(["S-1-5-21-1"]))


async def test_local_auth_allows_user_with_roles() -> None:
    config = LocalAuthConfig(
        users={"alice": "pw"},
        roles=RoleMappingConfig({"alice": ["admin"]}),
    )
    user = await LocalAuth(config).password_auth("alice", "pw")
    assert user is not None
    assert user.identifier == "alice"
    assert user.metadata[UserMetadataField.ROLES] == ["admin"]


async def test_local_auth_rejects_wrong_password() -> None:
    config = LocalAuthConfig(users={"alice": "pw"})
    user = await LocalAuth(config).password_auth("alice", "nope")
    assert user is None


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
    assert user is not None
    assert UserMetadataField.ROLES not in user.metadata


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
            bind_password="secret",
            mapping=mapping,
        ),
    )
    user = ADUserEntry(
        dn="CN=alice,OU=U",
        samaccountname="alice",
        member_of=["CN=Devs,OU=G", "CN=Other,OU=G"],
    )
    roles = list(provider.roles_of(user))
    assert sorted(roles) == ["admin", "dev", "devops"]


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
            bind_password="secret",
            mapping=mapping,
        ),
    )

    blocked_by_name = ADUserEntry(
        dn="CN=bob,OU=U",
        samaccountname="bob",
        member_of=[],
    )
    assert provider.excluded_of(blocked_by_name) is True

    blocked_by_group = ADUserEntry(
        dn="CN=carol,OU=U",
        samaccountname="carol",
        member_of=["CN=Blocked,OU=G"],
    )
    assert provider.excluded_of(blocked_by_group) is True

    allowed = ADUserEntry(
        dn="CN=carol,OU=U",
        samaccountname="carol",
        member_of=["CN=Other,OU=G"],
    )
    assert provider.excluded_of(allowed) is False
