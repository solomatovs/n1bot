"""Вход по паролю через AD стенда: живой LDAP.

Второй пользователь стенда (учётка служебного bind каталога) входит своим
паролем; роли ему не обязательны — проверяется сам путь bind + поиск +
допуск, а не состав групп.
"""

from __future__ import annotations

from typing import Any

import pytest
from omegaconf import DictConfig

from boba.auth.config import LdapAuthConfig
from boba.config import bind
from boba.identity.errors import AuthenticationError, AuthorizationError
from boba.identity.principal import IdentityRule
from boba.identity.session import SignInProvider, UserLogin
from boba.identity.signin import PasswordSignIn
from boba.stand.signin import SignInStand
from boba.stand.site import Stand

STAND = Stand.required()

pytestmark = [
    pytest.mark.integration,
    pytest.mark.anyio,
    pytest.mark.failif(not STAND.live(), reason="нет keytab/krb5.conf локального AD"),
]


def _reader_login() -> str:
    _, _, name = STAND.reader_bind.user.rpartition(Stand.NETBIOS_SEPARATOR)

    return name


def _ldap_config(raw_config: DictConfig) -> LdapAuthConfig:
    config = bind(raw_config, path="auth.ldap", model=LdapAuthConfig)

    return config.model_copy(update={"require_roles": False})


def _ldap_sign_in(raw_config: DictConfig) -> PasswordSignIn:
    signed = SignInStand.assembly().password([_ldap_config(raw_config)])
    if signed is None:
        raise AssertionError("an ldap config yields a password sign-in")

    return signed


async def test_reader_signs_in_with_the_directory_password(raw_config: Any) -> None:
    signed = await _ldap_sign_in(raw_config).sign_in(
        _reader_login(), STAND.reader_bind.password.get_secret_value()
    )

    assert signed is not None
    expected = _ldap_config(raw_config).identity.identity(
        UserLogin.of(_reader_login()).key
    )
    assert signed.identifier == expected.identifier
    assert signed.sign_in.provider == SignInProvider.LDAP.value


async def test_wrong_password_is_refused(raw_config: Any) -> None:
    with pytest.raises(AuthenticationError, match="Invalid username or password"):
        await _ldap_sign_in(raw_config).sign_in(_reader_login(), "wrong")


async def test_unknown_login_is_refused(raw_config: Any) -> None:
    with pytest.raises(AuthenticationError):
        await _ldap_sign_in(raw_config).sign_in("nobody-here", "x")


async def test_two_directories_are_separate_realms(raw_config: DictConfig) -> None:
    office = _ldap_config(raw_config).model_copy(
        update={"identity": IdentityRule(realm="office")}
    )
    partner = office.model_copy(update={"identity": IdentityRule(realm="partner")})
    providers = SignInStand.assembly().password([office, partner])
    assert providers is not None
    password = STAND.reader_bind.password.get_secret_value()
    with pytest.raises(AuthorizationError, match="select an identity realm"):
        await providers.sign_in(_reader_login(), password)
    first = await providers.sign_in(_reader_login(), password, "office")
    second = await providers.sign_in(_reader_login(), password, "partner")
    assert first is not None
    assert second is not None
    assert first.identifier == f"office:{_reader_login().lower()}"
    assert second.identifier == f"partner:{_reader_login().lower()}"
