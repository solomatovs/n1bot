from fastapi import FastAPI

from boba.auth.composite import PasswordAuthCallbackInstaller
from boba.auth.config import AuthConfig
from boba.auth.kerberos import KerberosAuth, KerberosAuthConfig
from boba.auth.ldap import LdapAuth, LdapAuthConfig
from boba.auth.local import LocalAuth, LocalAuthConfig


class ChainlitAuthInstaller:
    """Единая точка подключения авторизации; стратегия выбирается конфигом."""

    def __init__(self, url_prefix: str, configs: list[AuthConfig]) -> None:
        self._url_prefix = url_prefix
        self._configs = configs

    def install(self, chainlit_app: FastAPI) -> KerberosAuth | None:
        """Ставит выбранные способы авторизации; возвращает kerberos, если он есть.

        KerberosAuth нужен вызывающему ради delegation (ccache пользователя
        для tools), у остальных способов наблюдаемого состояния нет.
        """
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

        # устанавливаю password callback если есть хотя бы один из вариантов
        password_callback.install_callback_if_any_exists()

        return kerberos
