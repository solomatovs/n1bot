"""Вход по паролю через AD стенда: живой LDAP.

Второй пользователь стенда (учётка служебного bind каталога) входит своим
паролем; роли ему не обязательны — проверяется сам путь bind + поиск +
допуск, а не состав групп.
"""

from __future__ import annotations

from typing import Any

import pytest
from omegaconf import DictConfig

from boba.auth.config import LdapAuthConfig, LocalAuthConfig
from boba.config import bind
from boba.identity.errors import AuthenticationError
from boba.identity.session import UserLogin
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
    signed = SignInStand.backends().password([_ldap_config(raw_config)])
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
    assert signed.provider == LdapAuthConfig.PROVIDER


async def test_wrong_password_is_refused(raw_config: Any) -> None:
    with pytest.raises(AuthenticationError, match="Invalid username or password"):
        await _ldap_sign_in(raw_config).sign_in(_reader_login(), "wrong")


async def test_unknown_login_is_refused(raw_config: Any) -> None:
    with pytest.raises(AuthenticationError):
        await _ldap_sign_in(raw_config).sign_in("nobody-here", "x")


async def test_directory_and_local_sign_in_share_the_same_identity(
    raw_config: DictConfig,
) -> None:
    directory = _ldap_config(raw_config)
    local = LocalAuthConfig(
        users={_reader_login(): "local-password"}, require_roles=False
    )
    providers = SignInStand.backends().password([local, directory])
    assert providers is not None
    first = await providers.sign_in(_reader_login(), "local-password")
    second = await providers.sign_in(
        _reader_login(), STAND.reader_bind.password.get_secret_value()
    )
    assert first is not None
    assert second is not None
    assert first.identity == second.identity
    assert first.provider == LocalAuthConfig.PROVIDER
    assert second.provider == LdapAuthConfig.PROVIDER
