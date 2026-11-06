import os
from collections.abc import Sequence
from pathlib import Path

from chainlit.auth import get_configuration
from chainlit.config import config as chainlit_config
from fastapi import FastAPI

from boba.aid.auth.composite import PasswordCallback, SessionCallback
from boba.aid.auth.kerberos import KerberosAuth
from boba.aid.auth.proxy import ProxyAuth
from boba.aid.auth.refresh import PageUrls, SessionRefresh
from boba.aid.auth.session import BrowserSessions
from boba.aid.infra.session import ChainlitSessions
from boba.auth.config import KerberosAuthConfig, ProxyAuthConfig


class ChainlitSessionTtl:
    """Срок JWT и cookie chainlit — [session].session_ttl_sec, а не toml chainlit."""

    @staticmethod
    def apply(ttl_sec: int) -> None:
        chainlit_config.project.user_session_timeout = ttl_sec


class ChainlitAuthInstaller:
    """Единая точка подключения авторизации поверх сервиса входа."""

    def __init__(  # noqa: PLR0913 — установка входа собирается всеми зависимостями сразу
        self,
        url_prefix: str,
        kerberos: Sequence[KerberosAuthConfig],
        proxy: Sequence[ProxyAuthConfig],
        auth: BrowserSessions,
        session_ttl_sec: int,
        sessions: ChainlitSessions,
        app_root: Path,
    ) -> None:
        self._url_prefix = url_prefix
        self._kerberos = tuple(kerberos)
        self._proxy = tuple(proxy)
        self._auth = auth
        self._session_ttl_sec = session_ttl_sec
        self._sessions = sessions
        self._app_root = app_root
        self._session_callback = SessionCallback(auth, sessions)

    def install(self, chainlit_app: FastAPI) -> None:
        """Ставит способы входа: SSO-роуты, proxy-роут и парольный маршрут."""
        os.environ[KerberosAuth.CUSTOM_AUTH_ENV] = "1"
        ChainlitSessionTtl.apply(self._session_ttl_sec)

        self._session_callback.install(chainlit_app)

        sso_path = ""
        if len(self._kerberos) == 1:
            sso_path = self._kerberos[0].sso_path

        urls = PageUrls.of(self._url_prefix, sso_path)
        SessionRefresh(urls, self._auth, self._sessions, self._app_root).install(
            chainlit_app
        )

        providers = self._auth.providers()
        for kerberos in self._kerberos:
            KerberosAuth(kerberos.sso_path, urls, self._auth).install(chainlit_app)

        for proxy in self._proxy:
            ProxyAuth(proxy, self._auth).install(chainlit_app)

        if providers.password:
            PasswordCallback(self._auth).install(chainlit_app)

        SessionRefresh.prepend_route(chainlit_app, "/auth/config", self.configuration)

    async def configuration(self) -> dict[str, object]:
        config = get_configuration()
        config["passwordAuth"] = self._auth.providers().password

        return config
