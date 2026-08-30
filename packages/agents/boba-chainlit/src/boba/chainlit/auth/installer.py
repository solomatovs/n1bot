from fastapi import FastAPI

from boba.auth.config import AuthConfig, KerberosAuthConfig
from boba.auth.signin import PasswordSignIns
from boba.chainlit.auth.composite import PasswordCallback
from boba.chainlit.auth.kerberos import KerberosAuth
from chainlit.config import config as chainlit_config


class ChainlitSessionTtl:
    """Срок JWT и cookie chainlit — [session].session_ttl_sec, а не toml chainlit."""

    @staticmethod
    def apply(ttl_sec: int) -> None:
        chainlit_config.project.user_session_timeout = ttl_sec


class ChainlitAuthInstaller:
    """Единая точка подключения авторизации; стратегия выбирается конфигом."""

    def __init__(
        self, url_prefix: str, configs: list[AuthConfig], session_ttl_sec: int
    ) -> None:
        self._url_prefix = url_prefix
        self._configs = configs
        self._session_ttl_sec = session_ttl_sec

    def install(self, chainlit_app: FastAPI) -> KerberosAuth | None:
        "Ставит способы авторизации; KerberosAuth нужен ради delegation в tools"
        ChainlitSessionTtl.apply(self._session_ttl_sec)

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
