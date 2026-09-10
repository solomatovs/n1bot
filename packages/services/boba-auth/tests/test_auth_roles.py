"""Роли и исключения авторизации: правило допуска без KDC/LDAP.

Интеграционный путь (реальный SPNEGO/AD) в тестовом окружении недоступен,
поэтому проверяется чистая логика маппинга SID/ролей и решений LocalSignIn.
"""

from __future__ import annotations

import pytest
from pydantic import SecretStr

from boba.auth.config import (
    DirectoryRolesConfig,
    KerberosRoleProviders,
    LdapRolesConfig,
    LocalAuthConfig,
    LocalRoleProviders,
    LocalRolesConfig,
    PrincipalRolesConfig,
)
from boba.auth.roles import PrincipalRoles, RoleProviders
from boba.identity.admission import (
    PrincipalFacts,
    RoleExcludeConfig,
    RoleMappingConfig,
    RoleRules,
)
from boba.identity.errors import AuthorizationError
from boba.identity.session import UserLogin
from boba.identity.signin import PasswordSignIn
from boba.stand.signin import SignInStand
from boba.stand_core.fakes import FakeSecret

pytestmark = pytest.mark.anyio


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    pass


def _local_sign_in(config: LocalAuthConfig) -> PasswordSignIn:
    """Вход по конфигу через сборку стенда: тест — точка bootstrap."""
    signed = SignInStand.assembly().password([config])
    if signed is None:
        raise AssertionError("a local config yields a password sign-in")

    return signed


def _local_roles(
    mapping: dict[str, list[str]] | None = None, exclude: list[str] | None = None
) -> LocalRoleProviders:
    if mapping is None:
        mapping = {}

    if exclude is None:
        exclude = []

    return LocalRoleProviders(
        local=LocalRolesConfig(
            mapping=RoleMappingConfig(mapping), exclude=RoleExcludeConfig(exclude)
        )
    )


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
        roles=_local_roles(mapping={"alice": ["admin"]}),
    )
    user = await _local_sign_in(config).sign_in("alice", "pw")
    if user is None:
        raise AssertionError("user is not None")
    if user.identifier != "alice":
        raise AssertionError('user.identifier == "alice"')
    if user.sign_in.roles != frozenset({"admin"}):
        raise AssertionError('user.sign_in.roles == {"admin"}')


async def test_local_auth_rejects_wrong_password() -> None:
    config = LocalAuthConfig(users={"alice": "pw"})
    user = await _local_sign_in(config).sign_in("alice", "nope")
    if user is not None:
        raise AssertionError("user is None")


async def test_local_auth_rejects_excluded_user() -> None:
    config = LocalAuthConfig(
        users={"alice": "pw"},
        roles=_local_roles(exclude=["alice"]),
    )
    with pytest.raises(AuthorizationError):
        await _local_sign_in(config).sign_in("alice", "pw")


async def test_local_auth_rejects_no_roles_when_required() -> None:
    config = LocalAuthConfig(users={"alice": "pw"})
    with pytest.raises(AuthorizationError):
        await _local_sign_in(config).sign_in("alice", "pw")


async def test_local_auth_allows_no_roles_when_not_required() -> None:
    config = LocalAuthConfig(users={"alice": "pw"}, require_roles=False)
    user = await _local_sign_in(config).sign_in("alice", "pw")
    if user is None:
        raise AssertionError("user is not None")
    if user.sign_in.roles:
        raise AssertionError("no roles expected")


def test_ldap_rules_map_roles_from_all_sources() -> None:
    mapping = DirectoryRolesConfig(
        samaccountname=RoleMappingConfig({"alice": ["admin"]}),
        member_of=RoleMappingConfig({"CN=Devs,OU=G": ["dev"]}),
        dn=RoleMappingConfig({"CN=alice,OU=U": ["devops"]}),
    )
    facts = PrincipalFacts(
        login="alice",
        dn="CN=alice,OU=U",
        member_of=("CN=Devs,OU=G", "CN=Other,OU=G"),
    )
    if mapping.rules().admit(facts) != ["admin", "dev", "devops"]:
        raise AssertionError('admit(facts) == ["admin", "dev", "devops"]')


def test_ldap_rules_exclude_by_any_source() -> None:
    rules = DirectoryRolesConfig(
        samaccountname_ex=RoleExcludeConfig(["bob"]),
        member_of_ex=RoleExcludeConfig(["CN=Blocked,OU=G"]),
    ).rules()

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


async def test_kerberos_providers_join_principal_and_directory_roles() -> None:
    """Провайдеры principal и ldap складывают роли; каталог здесь заменён
    фактами с уже известными группами через правила directory."""
    ldap = LdapRolesConfig(
        server="ldaps://dc.example.com:636",
        base_dn="DC=example,DC=com",
        bind_dn="cn=svc",
        bind_password=SecretStr(FakeSecret.LDAP_BIND),
        mapping=DirectoryRolesConfig(
            member_of=RoleMappingConfig({"CN=Devs,OU=G": ["dev"]})
        ),
    )
    principal = PrincipalRolesConfig(principal=RoleMappingConfig({"alice@X": ["adm"]}))
    providers = KerberosRoleProviders(principal=principal, ldap=ldap)
    facts = PrincipalFacts(principal="alice@X", member_of=("CN=Devs,OU=G",))

    by_principal = await RoleProviders([PrincipalRoles(principal)], True).admit(facts)
    if by_principal != ["adm"]:
        raise AssertionError(f'principal provider alone gives ["adm"]: {by_principal}')

    by_directory_rules = ldap.mapping.rules().admit(facts)
    if by_directory_rules != ["dev"]:
        raise AssertionError(f'directory rules give ["dev"]: {by_directory_rules}')

    if providers.ldap is None or providers.principal is None:
        raise AssertionError("both providers stay configured")


async def test_no_providers_with_require_roles_refuses() -> None:
    providers = RoleProviders([], require_roles=True)

    with pytest.raises(AuthorizationError, match="no role came"):
        await providers.admit(PrincipalFacts(login="alice"))


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
            roles=_local_roles(mapping={"Maksimov.MA": ["admin"]}),
        )

        user = await _local_sign_in(config).sign_in("Maksimov.MA", "pw")

        if user is None:
            raise AssertionError("user is not None")

        if user.identifier != "maksimov.ma":
            raise AssertionError(f"identifier канонизирован, дано {user.identifier!r}")

        if user.display_name != "Maksimov.MA":
            raise AssertionError(f"display как в конфиге, дано {user.display_name!r}")
