from pathlib import Path

from fastapi import FastAPI

from boba.auth import AuthService
from boba.chainlit.auth.composite import PasswordCallback
from boba.chainlit.auth.kerberos import KerberosAuth
from boba.chainlit.auth.refresh import PageUrls, SessionRefresh
from boba.chainlit.infra.session import ChainlitSessions
from chainlit.config import config as chainlit_config


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
        sso_path: str,
        auth: AuthService,
        session_ttl_sec: int,
        sessions: ChainlitSessions,
        app_root: Path,
    ) -> None:
        self._url_prefix = url_prefix
        self._sso_path = sso_path
        self._auth = auth
        self._session_ttl_sec = session_ttl_sec
        self._sessions = sessions
        self._app_root = app_root

    def install(self, chainlit_app: FastAPI) -> None:
        """Ставит способы входа: SSO-роуты и password-callback."""
        ChainlitSessionTtl.apply(self._session_ttl_sec)

        urls = PageUrls.of(self._url_prefix, self._sso_path)
        SessionRefresh(urls, self._auth, self._sessions, self._app_root).install(
            chainlit_app
        )

        providers = self._auth.providers()
        if providers.sso:
            KerberosAuth(self._sso_path, urls, self._auth).install(chainlit_app)

        if providers.password:
            PasswordCallback(self._auth).install()
