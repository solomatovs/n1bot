"""SSO через Kerberos/SPNEGO в chainlit: роуты входа и обновления над общим обменом.

Обмен, допуск и билет — boba.auth.sso; здесь cl.User, JWT-cookie chainlit,
кнопка на странице логина и подмена токена живым сокет-сессиям.

Ошибки: AuthenticationError, AuthorizationError — отказ входа;
ExternalServiceError — недоступен внешний сервис (KDC, LDAP);
InternalServiceError — keytab/SPN/конфиг непригодны.
"""

import logging
import os
from collections.abc import Awaitable, Callable, Sequence
from enum import StrEnum
from pathlib import Path
from typing import Any, ClassVar

from fastapi import FastAPI
from pydantic import BaseModel
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response

import chainlit as cl
from boba.auth import AuthService, IssuedSession
from boba.chainlit.infra.session import ChainlitSessions
from boba.identity.errors import (
    AuthorizationError,
    BaseError,
    FailureReport,
)
from boba.identity.session import LogLine
from boba.identity.signin import SignedIn
from boba.identity.sso import (
    OwnRequest,
    SsoChallenge,
    SsoErrorCode,
    SsoRefused,
)
from boba.runtime.http import SsoRequests
from chainlit.auth import set_auth_cookie
from chainlit.config import config as cl_config


class ButtonJsVar(StrEnum):
    """Плейсхолдеры sso_button.js, которые сервер заменяет на URL."""

    SSO_URL = "__SSO_URL__"
    REFRESH_URL = "__REFRESH_URL__"
    REFRESH_HEADER = "__REFRESH_HEADER__"
    REFRESH_HEADER_VALUE = "__REFRESH_HEADER_VALUE__"
    TRANSLATIONS_URL = "__TRANSLATIONS_URL__"


class SsoUrls(BaseModel):
    """Адреса SSO с учётом url_prefix: роут, обновление, скрипт, логин, чат."""

    sso: str
    refresh: str
    js: str
    translations: str
    login: str
    app: str

    @classmethod
    def of(cls, url_prefix: str, sso_path: str) -> "SsoUrls":
        sso = f"{url_prefix}{sso_path}"

        return cls(
            sso=sso,
            refresh=f"{sso}/refresh",
            js=f"{sso}.js",
            translations=f"{url_prefix}/project/translations",
            login=f"{url_prefix}/login",
            app=f"{url_prefix}/",
        )


class KerberosAuth:
    """SSO через Kerberos/SPNEGO: кнопка на /login ведёт на /auth/sso.

    Собран на FastAPI без chainlit header-auth: вход по явной кнопке, не автоматом.
    """

    _BUTTON_JS: ClassVar[Path] = Path(__file__).parent / "sso_button.js"
    """JS кнопки едет в wheel как package-data — см. pyproject boba-chainlit."""

    _CUSTOM_AUTH_ENV: ClassVar[str] = "CHAINLIT_CUSTOM_AUTH"
    """Флаг chainlit: вход обязателен, хотя свой колбэк авторизации не задан."""

    def __init__(self, url_prefix: str, sso_path: str, auth: AuthService) -> None:
        # роуты без префикса (root_path учтёт роутер), кнопка — с полным
        self._sso_path = sso_path
        self._urls = SsoUrls.of(url_prefix, sso_path)
        self.auth = auth
        self._logger = logging.getLogger(KerberosAuth.__name__)

    def install(self, chainlit_app: FastAPI) -> None:
        # без password/header-колбэка chainlit считает, что логина нет, и пускает
        # анонима; флаг включает обязательный вход без автозапроса /auth/header
        os.environ[self._CUSTOM_AUTH_ENV] = "1"

        self._install_routes(chainlit_app)
        self._install_button_js()

    @staticmethod
    def user_of(signed: SignedIn) -> cl.User:
        return cl.User(
            identifier=signed.identifier,
            display_name=signed.display_name,
            metadata=dict(signed.metadata),
        )

    def _login_redirect(self, exc: BaseError) -> RedirectResponse:
        """Исход SSO кодом на страницу логина: браузер пришёл навигацией, не fetch."""
        self._logger.error("%s", LogLine.safe(FailureReport.of(exc).log))

        code = SsoErrorCode.FAILED
        if isinstance(exc, AuthorizationError):
            code = SsoErrorCode.DENIED

        return RedirectResponse(url=code.login_url(self._urls.login), status_code=303)

    def _challenge(self) -> Response:
        """401 Negotiate: с тикетом браузер повторит сам, без него уйдёт на логин."""
        return Response(
            content=SsoErrorCode.TICKET.challenge_page(self._urls.login),
            status_code=401,
            headers=SsoChallenge.HEADERS,
            media_type="text/html",
        )

    async def auth_sso(self, request: Request) -> Response:
        """Вход: SPNEGO → строка users и токен сервисом входа → cookie → в чат."""
        try:
            outcome = await self.auth.by_spnego(SsoRequests.of(request))
        except BaseError as exc:
            return self._login_redirect(exc)

        if isinstance(outcome, SsoChallenge):
            return self._challenge()

        response = RedirectResponse(url=self._urls.app, status_code=303)
        set_auth_cookie(request, response, outcome.token)

        return response

    async def refresh(self, request: Request) -> Response:
        """Повторный SPNEGO живой сессии: новый JWT с новым билетом для её сокетов."""
        from chainlit.auth.cookie import get_token_from_cookies  # noqa: PLC0415

        token = get_token_from_cookies(request.cookies)
        outcome = await self.auth.refresh(SsoRequests.of(request), token)
        if isinstance(outcome, SsoRefused):
            return Response(status_code=403)

        if isinstance(outcome, SsoChallenge):
            # 401 без страницы логина: ответ читает скрипт, не человек
            return Response(status_code=401, headers=SsoChallenge.HEADERS)

        return self._adopted(request, outcome)

    def _adopted(self, request: Request, session: IssuedSession) -> Response:
        response = Response(status_code=204)
        set_auth_cookie(request, response, session.token)
        identifier = session.signed.identifier
        adopted = ChainlitSessions().adopt_token(identifier, session.token)
        self._logger.info(
            "kerberos: sessions adopted the refreshed JWT [user=%s] [sessions=%d]",
            session.signed.identifier,
            adopted,
        )

        return response

    def _install_routes(self, chainlit_app: FastAPI) -> None:
        """Регистрирует /auth/sso, /auth/sso/refresh и /sso.js (кнопка)."""
        js = self._get_static_button()

        async def sso_js() -> Response:
            return Response(content=js, media_type="application/javascript")

        self._prepend_route(chainlit_app, self._sso_path, self.auth_sso)
        self._prepend_route(
            chainlit_app, f"{self._sso_path}/refresh", self.refresh, methods=["POST"]
        )
        self._prepend_route(chainlit_app, self._urls.js, sso_js)

    def _install_button_js(self) -> None:
        """Подключает sso.js на странице логина через custom_js."""
        existing = cl_config.ui.custom_js
        if not existing:
            cl_config.ui.custom_js = self._urls.js
            return

        if existing == self._urls.js:
            return

        # слот один на приложение: занявший его скрипт обязан подгрузить sso.js
        self._logger.info(
            "custom_js already set (%s) — expecting it to load %s itself",
            existing,
            self._urls.js,
        )

    @staticmethod
    def _prepend_route(
        chainlit_app: FastAPI,
        path: str,
        endpoint: Callable[..., Awaitable[Any]],
        methods: Sequence[str] = ("GET",),
    ) -> None:
        """Добавляет роут в начало, иначе его перехватит chainlit"""
        chainlit_app.add_api_route(
            path, endpoint, methods=list(methods), include_in_schema=False
        )

        route = chainlit_app.router.routes.pop()
        chainlit_app.router.routes.insert(0, route)

    def _get_static_button(self) -> str:
        """JS кнопки SSO из файла-ресурса рядом с модулем; сервер знает только URL."""
        template = self._BUTTON_JS.read_text(encoding="utf-8")
        with_sso = template.replace(ButtonJsVar.SSO_URL, self._urls.sso)
        with_refresh = with_sso.replace(ButtonJsVar.REFRESH_URL, self._urls.refresh)
        with_header = with_refresh.replace(
            ButtonJsVar.REFRESH_HEADER, OwnRequest.HEADER
        )
        with_value = with_header.replace(
            ButtonJsVar.REFRESH_HEADER_VALUE, OwnRequest.VALUE
        )

        return with_value.replace(ButtonJsVar.TRANSLATIONS_URL, self._urls.translations)
