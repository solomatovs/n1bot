"""Вход по паролю через AD стенда и факты каталога для SSO-ролей: живой LDAP.

Второй пользователь стенда (ldap_bind_user) входит своим паролем; роли ему
не обязательны — проверяется сам путь bind + поиск + допуск, а не состав групп.
"""

from __future__ import annotations

from typing import Any

import pytest
from omegaconf import DictConfig

from boba.auth.config import KerberosRolesInLdapConfig, LdapAuthConfig
from boba.auth.signin import LdapSignIn
from boba.auth.sso import KerberosRolesInLdapProvider
from boba.config import bind
from boba.identity.admission import PrincipalFacts
from boba.identity.errors import AuthenticationError
from boba.identity.session import SignInProvider, UserLogin
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


def _ldap_config(raw_config: DictConfig) -> LdapAuthConfig:
    config = bind(raw_config, path="auth.ldap", model=LdapAuthConfig)

    return config.model_copy(update={"require_roles": False})


async def test_reader_signs_in_with_the_directory_password(raw_config: Any) -> None:
    signed = await LdapSignIn(_ldap_config(raw_config), Ldap3Directory()).sign_in(
        _reader_login(), STAND.ldap_bind_password.get_secret_value()
    )

    assert signed is not None
    assert signed.identifier == UserLogin.of(_reader_login()).key
    assert signed.sign_in.provider == SignInProvider.LDAP.value


async def test_wrong_password_is_refused(raw_config: Any) -> None:
    with pytest.raises(AuthenticationError, match="Invalid username or password"):
        await LdapSignIn(_ldap_config(raw_config), Ldap3Directory()).sign_in(
            _reader_login(), "wrong"
        )


async def test_unknown_login_is_refused(raw_config: Any) -> None:
    with pytest.raises(AuthenticationError):
        await LdapSignIn(_ldap_config(raw_config), Ldap3Directory()).sign_in(
            "nobody-here", "x"
        )


async def test_directory_facts_of_the_sso_principal(raw_config: Any) -> None:
    """Факты для допуска SSO: DN, sAMAccountName и группы по UPN принципала."""
    config = bind(
        raw_config, path="auth.kerberos.ldap_roles", model=KerberosRolesInLdapConfig
    )

    entry = await KerberosRolesInLdapProvider(config, Ldap3Directory()).request(
        STAND.reader_principal
    )

    assert entry.samaccountname.lower() == _reader_login().lower()
    assert entry.dn.lower().endswith(STAND.ldap_base_dn.lower())

    facts = PrincipalFacts(
        principal=STAND.reader_principal,
        login=entry.samaccountname,
        dn=entry.dn,
        member_of=tuple(entry.member_of),
    )
    roles = config.mapping.rules(require_roles=False).admit(facts)
    assert isinstance(roles, list)
