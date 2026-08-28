from fastapi import FastAPI

from boba.chainlit.auth.composite import PasswordCallback
from boba.chainlit.auth.kerberos import KerberosAuth
from boba.runtime.auth_config import AuthConfig, KerberosAuthConfig
from boba.runtime.signin import PasswordSignIns


class ChainlitAuthInstaller:
    """Единая точка подключения авторизации; стратегия выбирается конфигом."""

    def __init__(self, url_prefix: str, configs: list[AuthConfig]) -> None:
        self._url_prefix = url_prefix
        self._configs = configs

    def install(self, chainlit_app: FastAPI) -> KerberosAuth | None:
        "Ставит способы авторизации; KerberosAuth нужен ради delegation в tools"
        kerberos: KerberosAuth | None = None

        for auth in self._configs:
            if not isinstance(auth, KerberosAuthConfig):
                continue

            if kerberos is not None:
                # две SpnegoMiddleware на один sso_path — ошибка конфига
                raise ValueError("kerberos authorization configured twice")

            kerberos = KerberosAuth(self._url_prefix, auth)
            kerberos.install(chainlit_app)

        if signin := PasswordSignIns.of(self._configs):
            PasswordCallback(signin).install()

        return kerberos
