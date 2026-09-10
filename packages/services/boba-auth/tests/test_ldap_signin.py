"""Вход по паролю через AD стенда: живой LDAP.

Второй пользователь стенда (ldap_bind_user) входит своим паролем; роли ему
не обязательны — проверяется сам путь bind + поиск + допуск, а не состав групп.
"""

from __future__ import annotations

from typing import Any

import pytest
from omegaconf import DictConfig

from boba.auth.config import LdapAuthConfig
from boba.config import bind
from boba.identity.errors import AuthenticationError
from boba.identity.session import SignInProvider, UserLogin
from boba.identity.signin import PasswordSignIn
from boba.stand.signin import SignInStand
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


def _ldap_sign_in(raw_config: DictConfig) -> PasswordSignIn:
    signed = SignInStand.assembly().password([_ldap_config(raw_config)])
    if signed is None:
        raise AssertionError("an ldap config yields a password sign-in")

    return signed


async def test_reader_signs_in_with_the_directory_password(raw_config: Any) -> None:
    signed = await _ldap_sign_in(raw_config).sign_in(
        _reader_login(), STAND.ldap_bind_password.get_secret_value()
    )

    assert signed is not None
    assert signed.identifier == UserLogin.of(_reader_login()).key
    assert signed.sign_in.provider == SignInProvider.LDAP.value


async def test_wrong_password_is_refused(raw_config: Any) -> None:
    with pytest.raises(AuthenticationError, match="Invalid username or password"):
        await _ldap_sign_in(raw_config).sign_in(_reader_login(), "wrong")


async def test_unknown_login_is_refused(raw_config: Any) -> None:
    with pytest.raises(AuthenticationError):
        await _ldap_sign_in(raw_config).sign_in("nobody-here", "x")
