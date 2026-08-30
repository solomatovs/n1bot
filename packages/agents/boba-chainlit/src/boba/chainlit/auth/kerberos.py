"""SSO через Kerberos/SPNEGO в chainlit: роуты входа и обновления над общим обменом.

Обмен, допуск и билет — boba.auth.sso; здесь cl.User, JWT-cookie chainlit,
кнопка на странице логина и подмена токена живым сокет-сессиям.

Ошибки: AuthenticationError, AuthorizationError — отказ входа;
ExternalServiceError — недоступен внешний сервис (KDC, LDAP);
InternalServiceError — keytab/SPN/конфиг непригодны.
"""

import logging
import os
from typing import ClassVar

from fastapi import FastAPI
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response

import chainlit as cl
from boba.auth import AuthService
from boba.chainlit.auth.refresh import PageUrls, SessionRefresh
from boba.identity.errors import (
    AuthorizationError,
    BaseError,
    FailureReport,
)
from boba.identity.session import LogLine
from boba.identity.signin import SignedIn
from boba.identity.sso import (
    SsoChallenge,
    SsoErrorCode,
)
from boba.runtime.http import SessionCookie, SsoRequests, SsoResponses


class KerberosAuth:
    """SSO через Kerberos/SPNEGO: кнопка на /login ведёт на /auth/sso.

    Собран на FastAPI без chainlit header-auth: вход по явной кнопке, не автоматом.
    Обновление сессии и скрипт страницы — у SessionRefresh, общего для всех входов.
    """

    _CUSTOM_AUTH_ENV: ClassVar[str] = "CHAINLIT_CUSTOM_AUTH"
    """Флаг chainlit: вход обязателен, хотя свой колбэк авторизации не задан."""

    def __init__(self, sso_path: str, urls: PageUrls, auth: AuthService) -> None:
        # роут без префикса (root_path учтёт роутер), адреса страницы — с полным
        self._sso_path = sso_path
        self._urls = urls
        self.auth = auth
        self._logger = logging.getLogger(KerberosAuth.__name__)

    def install(self, chainlit_app: FastAPI) -> None:
        # без password/header-колбэка chainlit считает, что логина нет, и пускает
        # анонима; флаг включает обязательный вход без автозапроса /auth/header
        os.environ[self._CUSTOM_AUTH_ENV] = "1"

        SessionRefresh.prepend_route(chainlit_app, self._sso_path, self.auth_sso)

    @staticmethod
    def user_of(signed: SignedIn) -> cl.User:
        return cl.User(
            identifier=signed.identifier,
            display_name=signed.display_name,
            metadata=signed.sign_in.render(),
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
        return SsoResponses.challenge(self._urls.login)

    async def auth_sso(self, request: Request) -> Response:
        """Вход: SPNEGO → строка users и токен сервисом входа → cookie → в чат."""
        try:
            outcome = await self.auth.by_spnego(SsoRequests.of(request))
        except BaseError as exc:
            return self._login_redirect(exc)

        if isinstance(outcome, SsoChallenge):
            return self._challenge()

        response = RedirectResponse(url=self._urls.app, status_code=303)
        SessionCookie(self.auth.cookie()).put(response, request.cookies, outcome.token)

        return response
