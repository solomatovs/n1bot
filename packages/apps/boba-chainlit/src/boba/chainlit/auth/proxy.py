"""Вход по доверенному заголовку в chainlit: маршрут [auth.proxy].path над сервисом
входа. Бэкенд партнёра зовёт его сам, ответ — cookie сессии без редиректов.

Ошибки: свои не выпускает — ошибки сервиса входа (BaseError) переводит в HTTP
DomainErrorMiddleware: 401 подпись или окно, 403 адрес или роли.
"""

import logging
import os

from fastapi import FastAPI
from starlette.requests import Request
from starlette.responses import Response

from boba.auth import AuthService
from boba.auth.config import ProxyAuthConfig
from boba.chainlit.auth.kerberos import KerberosAuth
from boba.chainlit.auth.refresh import SessionRefresh
from boba.runtime.http import ProxyRequests, SessionCookie

__all__ = ["ProxyAuth"]


class ProxyAuth:
    """Маршрут POST [auth.proxy].path: заголовки → сервис входа → cookie, 204.

    Создаётся установщиком ChainlitAuthInstaller, когда в [auth] есть proxy;
    заголовки читает по именам из того же конфига.
    """

    def __init__(self, config: ProxyAuthConfig, auth: AuthService) -> None:
        self._config = config
        self._auth = auth
        self._logger = logging.getLogger(ProxyAuth.__name__)

    def install(self, chainlit_app: FastAPI) -> None:
        # без password/header-колбэка chainlit пускал бы анонима; флаг включает
        # обязательный вход без автозапроса /auth/header
        os.environ[KerberosAuth.CUSTOM_AUTH_ENV] = "1"

        SessionRefresh.prepend_route(
            chainlit_app, self._config.path, self.auth_proxy, methods=["POST"]
        )

    async def auth_proxy(self, request: Request) -> Response:
        proxy_request = ProxyRequests.of(request, self._config.header_names())
        session = await self._auth.by_proxy(proxy_request)

        self._logger.info(
            "proxy sign-in [user=%s] [client=%s] [roles=%s]",
            session.signed.identifier,
            proxy_request.client,
            ",".join(sorted(session.signed.sign_in.roles)),
        )

        response = Response(status_code=204)
        SessionCookie(self._auth.cookie()).put(response, request.cookies, session.token)

        return response
