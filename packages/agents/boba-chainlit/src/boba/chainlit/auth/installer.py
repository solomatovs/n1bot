from fastapi import FastAPI

from boba.auth import AuthService
from boba.auth.config import AuthConfig, KerberosAuthConfig
from boba.chainlit.auth.composite import PasswordCallback
from boba.chainlit.auth.kerberos import KerberosAuth
from chainlit.config import config as chainlit_config


class ChainlitSessionTtl:
    """Срок JWT и cookie chainlit — [session].session_ttl_sec, а не toml chainlit."""

    @staticmethod
    def apply(ttl_sec: int) -> None:
        chainlit_config.project.user_session_timeout = ttl_sec


class ChainlitAuthInstaller:
    """Единая точка подключения авторизации поверх сервиса входа."""

    def __init__(
        self,
        url_prefix: str,
        configs: list[AuthConfig],
        auth: AuthService,
        session_ttl_sec: int,
    ) -> None:
        self._url_prefix = url_prefix
        self._configs = configs
        self._auth = auth
        self._session_ttl_sec = session_ttl_sec

    def install(self, chainlit_app: FastAPI) -> None:
        """Ставит способы входа: SSO-роуты и password-callback."""
        ChainlitSessionTtl.apply(self._session_ttl_sec)

        providers = self._auth.providers()
        if providers.sso:
            KerberosAuth(self._url_prefix, self._sso_path(), self._auth).install(
                chainlit_app
            )

        if providers.password:
            PasswordCallback(self._auth).install()

    def _sso_path(self) -> str:
        found: KerberosAuthConfig | None = None
        for auth in self._configs:
            if not isinstance(auth, KerberosAuthConfig):
                continue

            if found is not None:
                # две SpnegoMiddleware на один sso_path — ошибка конфига
                raise ValueError("kerberos authorization configured twice")

            found = auth

        if found is None:
            raise ValueError("sso is configured without [auth.kerberos]")

        return found.sso_path
