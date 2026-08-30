"""Роли и исключения авторизации: правило допуска без KDC/LDAP.

Интеграционный путь (реальный SPNEGO/AD) в тестовом окружении недоступен,
поэтому проверяется чистая логика маппинга SID/ролей и решений LocalSignIn.
"""

from __future__ import annotations

import pytest
from pydantic import SecretStr

from boba.auth.config import (
    KerberosRolesConfig,
    KerberosRolesInLdapConfig,
    KerberosRolesInLdapMappingConfig,
    LocalAuthConfig,
)
from boba.auth.signin import LocalSignIn
from boba.identity.admission import (
    PrincipalFacts,
    RoleExcludeConfig,
    RoleMappingConfig,
    RoleRules,
)
from boba.identity.errors import AuthorizationError
from boba.identity.session import UserLogin
from boba.stand.fakes import FakeSecret

pytestmark = pytest.mark.anyio


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    pass


def test_sid_roles_maps_each_group() -> None:
    rules = RoleRules(
        require_roles=False,
        by_sid=RoleMappingConfig({"S-1-5-21-1": ["admin"], "S-1-5-21-2": ["dev"]}),
    )
    facts = PrincipalFacts(group_sids=("S-1-5-21-1", "S-1-5-21-2", "S-1-5-21-3"))
    if rules.admit(facts) != ["admin", "dev"]:
        raise AssertionError('rules.admit(facts) == ["admin", "dev"]')


def test_sid_exclude_refuses_matching_group() -> None:
    rules = RoleRules(require_roles=False, by_sid_ex=RoleExcludeConfig(["S-1-5-21-9"]))
    with pytest.raises(AuthorizationError):
        rules.admit(PrincipalFacts(group_sids=("S-1-5-21-9",)))
    if rules.admit(PrincipalFacts(group_sids=("S-1-5-21-1",))) != []:
        raise AssertionError("a group outside the exclusions must pass")


def test_sid_exclusions_need_a_parsed_pac() -> None:
    rules = RoleRules(require_roles=False, by_sid_ex=RoleExcludeConfig(["S-1-5-21-9"]))
    with pytest.raises(AuthorizationError):
        rules.admit(PrincipalFacts(principal="reader@X", pac_parsed=False))
    if RoleRules(require_roles=False).admit(PrincipalFacts(pac_parsed=False)) != []:
        raise AssertionError("without sid exclusions an unparsed PAC is allowed")


async def test_local_auth_allows_user_with_roles() -> None:
    config = LocalAuthConfig(
        users={"alice": "pw"},
        roles=RoleMappingConfig({"alice": ["admin"]}),
    )
    user = await LocalSignIn(config).sign_in("alice", "pw")
    if user is None:
        raise AssertionError("user is not None")
    if user.identifier != "alice":
        raise AssertionError('user.identifier == "alice"')
    if user.sign_in.roles != frozenset({"admin"}):
        raise AssertionError('user.sign_in.roles == {"admin"}')


async def test_local_auth_rejects_wrong_password() -> None:
    config = LocalAuthConfig(users={"alice": "pw"})
    user = await LocalSignIn(config).sign_in("alice", "nope")
    if user is not None:
        raise AssertionError("user is None")


async def test_local_auth_rejects_excluded_user() -> None:
    config = LocalAuthConfig(
        users={"alice": "pw"},
        roles_ex=RoleExcludeConfig(["alice"]),
    )
    with pytest.raises(AuthorizationError):
        await LocalSignIn(config).sign_in("alice", "pw")


async def test_local_auth_rejects_no_roles_when_required() -> None:
    config = LocalAuthConfig(users={"alice": "pw"})
    with pytest.raises(AuthorizationError):
        await LocalSignIn(config).sign_in("alice", "pw")


async def test_local_auth_allows_no_roles_when_not_required() -> None:
    config = LocalAuthConfig(users={"alice": "pw"}, require_roles=False)
    user = await LocalSignIn(config).sign_in("alice", "pw")
    if user is None:
        raise AssertionError("user is not None")
    if user.sign_in.roles:
        raise AssertionError("no roles expected")


def test_ldap_rules_map_roles_from_all_sources() -> None:
    mapping = KerberosRolesInLdapMappingConfig(
        samaccountname=RoleMappingConfig({"alice": ["admin"]}),
        member_of=RoleMappingConfig({"CN=Devs,OU=G": ["dev"]}),
        dn=RoleMappingConfig({"CN=alice,OU=U": ["devops"]}),
    )
    facts = PrincipalFacts(
        login="alice",
        dn="CN=alice,OU=U",
        member_of=("CN=Devs,OU=G", "CN=Other,OU=G"),
    )
    if mapping.rules(require_roles=True).admit(facts) != ["admin", "dev", "devops"]:
        raise AssertionError('admit(facts) == ["admin", "dev", "devops"]')


def test_ldap_rules_exclude_by_any_source() -> None:
    rules = KerberosRolesInLdapMappingConfig(
        samaccountname_ex=RoleExcludeConfig(["bob"]),
        member_of_ex=RoleExcludeConfig(["CN=Blocked,OU=G"]),
    ).rules(require_roles=False)

    with pytest.raises(AuthorizationError):
        rules.admit(PrincipalFacts(login="bob", dn="CN=bob,OU=U"))

    with pytest.raises(AuthorizationError):
        rules.admit(
            PrincipalFacts(
                login="carol", dn="CN=carol,OU=U", member_of=("CN=Blocked,OU=G",)
            )
        )

    allowed = PrincipalFacts(
        login="carol", dn="CN=carol,OU=U", member_of=("CN=Other,OU=G",)
    )
    if rules.admit(allowed) != []:
        raise AssertionError("a user outside the exclusions must pass")


def test_kerberos_rules_merge_principal_sid_and_directory() -> None:
    config = KerberosRolesInLdapConfig(
        server="ldaps://dc.example.com:636",
        base_dn="DC=example,DC=com",
        bind_dn="cn=svc",
        bind_password=SecretStr(FakeSecret.LDAP_BIND),
        mapping=KerberosRolesInLdapMappingConfig(
            member_of=RoleMappingConfig({"CN=Devs,OU=G": ["dev"]})
        ),
    )
    roles = KerberosRolesConfig(principal=RoleMappingConfig({"alice@X": ["adm"]}))
    rules = (
        RoleRules(require_roles=True)
        .merged(roles.rules(True))
        .merged(config.mapping.rules(True))
    )
    facts = PrincipalFacts(principal="alice@X", member_of=("CN=Devs,OU=G",))
    if rules.admit(facts) != ["adm", "dev"]:
        raise AssertionError('rules.admit(facts) == ["adm", "dev"]')


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


class TestLocalSignInIdentifier:
    """LocalSignIn: в базу уходит канон логина, в интерфейс — как в конфиге."""

    async def test_identifier_is_the_canonical_login(self) -> None:
        config = LocalAuthConfig(
            users={"Maksimov.MA": "pw"},
            roles=RoleMappingConfig({"Maksimov.MA": ["admin"]}),
        )

        user = await LocalSignIn(config).sign_in("Maksimov.MA", "pw")

        if user is None:
            raise AssertionError("user is not None")

        if user.identifier != "maksimov.ma":
            raise AssertionError(f"identifier канонизирован, дано {user.identifier!r}")

        if user.display_name != "Maksimov.MA":
            raise AssertionError(f"display как в конфиге, дано {user.display_name!r}")
