from fastapi import FastAPI

from boba.chainlit.auth.composite import PasswordAuthCallbackInstaller
from boba.chainlit.auth.kerberos import KerberosAuth
from boba.chainlit.auth.ldap import LdapAuth
from boba.chainlit.auth.local import LocalAuth
from boba.runtime.auth_config import (
    AuthConfig,
    KerberosAuthConfig,
    LdapAuthConfig,
    LocalAuthConfig,
)


class ChainlitAuthInstaller:
    """Единая точка подключения авторизации; стратегия выбирается конфигом."""

    def __init__(self, url_prefix: str, configs: list[AuthConfig]) -> None:
        self._url_prefix = url_prefix
        self._configs = configs

    def install(self, chainlit_app: FastAPI) -> KerberosAuth | None:
        "Ставит способы авторизации; KerberosAuth нужен ради delegation в tools"
        password_callback = PasswordAuthCallbackInstaller()
        kerberos: KerberosAuth | None = None

        for auth in self._configs:
            if isinstance(auth, KerberosAuthConfig):
                if kerberos is not None:
                    # две SpnegoMiddleware на один sso_path — ошибка конфига
                    raise ValueError("kerberos authorization configured twice")

                kerberos = KerberosAuth(self._url_prefix, auth)
                kerberos.install(chainlit_app)

            elif isinstance(auth, LocalAuthConfig):
                password_callback.local_auth_setup(LocalAuth(auth))

            elif isinstance(auth, LdapAuthConfig):
                password_callback.ldap_auth_setup(LdapAuth(auth))

            else:
                raise ValueError(f"unknown authorization type: {type(auth).__name__}")

        password_callback.install_callback_if_any_exists()

        return kerberos
