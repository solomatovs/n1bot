"""Провайдеры ролей с живым каталогом стенда: LdapRoles ищет запись под
служебным bind'ом и отдаёт роли по правилам directory."""

from __future__ import annotations

from typing import Any

import pytest

from boba.auth.config import LdapRolesConfig
from boba.auth.roles import DirectoryLookup, LdapRoles, MissingEntry
from boba.config import bind
from boba.identity.admission import PrincipalFacts
from boba.ldap import Ldap3Directory
from boba.stand.site import Stand

STAND = Stand.required()

pytestmark = [
    pytest.mark.integration,
    pytest.mark.anyio,
    pytest.mark.skipif(not STAND.live(), reason="нет keytab/krb5.conf локального AD"),
]


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    """Каталог опрашивается напрямую: сессия чата не нужна."""


def _reader_login() -> str:
    _, _, name = STAND.ldap_bind_user.rpartition(Stand.NETBIOS_SEPARATOR)

    return name


async def test_ldap_roles_find_the_entry_by_upn_and_map_its_roles(
    raw_config: Any,
) -> None:
    """Факты для допуска SSO: DN, sAMAccountName и группы по UPN принципала."""
    config = bind(raw_config, path="auth.kerberos.roles.ldap", model=LdapRolesConfig)
    provider = LdapRoles(
        config, Ldap3Directory(), DirectoryLookup.UPN, "sso roles", MissingEntry.REFUSE
    )

    entry = await provider.request(STAND.reader_principal)

    assert entry.samaccountname.lower() == _reader_login().lower()
    assert entry.dn.lower().endswith(STAND.ldap_base_dn.lower())

    facts = PrincipalFacts(
        principal=STAND.reader_principal,
        login=entry.samaccountname,
        dn=entry.dn,
        member_of=tuple(entry.member_of),
    )
    roles = config.mapping.rules().admit(facts)
    assert isinstance(roles, list)

    provided = await provider.roles_of(PrincipalFacts(principal=STAND.reader_principal))
    assert provided == frozenset(roles)
