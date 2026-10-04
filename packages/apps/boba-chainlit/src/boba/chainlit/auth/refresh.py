"""Обновление входа живой сессии chainlit: маршрут POST /auth/refresh и скрипт
страницы, который зовёт его по сигналу сервера и уводит на логин при отказе.

Каким способом обновляться — SPNEGO-обмен или перевыпуск JWT — решает сервис
входа по виду входа; здесь только HTTP: cookie, статусы и живые сокеты.

Ошибки:
RuntimeError — собранного скрипта страницы нет в app_root.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Sequence
from enum import StrEnum
from pathlib import Path
from typing import Any, ClassVar

from fastapi import FastAPI, Request, Response
from pydantic import BaseModel, ConfigDict

from boba.auth import AuthService, IssuedSession
from boba.chainlit.infra.session import ChainlitSessions
from boba.identity.sso import OwnRequest, SsoChallenge, SsoRefused
from boba.runtime.http import RequestTokens, SessionCookie, SsoRequests, SsoResponses
from chainlit.config import config as cl_config

__all__ = ["PageJsVar", "PageUrls", "SessionRefresh"]


class PageJsVar(StrEnum):
    """Плейсхолдеры page.js, которые сервер заменяет на адреса и заголовок."""

    SSO_URL = "__SSO_URL__"
    REFRESH_URL = "__REFRESH_URL__"
    LOGIN_URL = "__LOGIN_URL__"
    REFRESH_HEADER = "__REFRESH_HEADER__"
    REFRESH_HEADER_VALUE = "__REFRESH_HEADER_VALUE__"
    TRANSLATIONS_URL = "__TRANSLATIONS_URL__"


class PageUrls(BaseModel):
    """Адреса входа с учётом url_prefix: SSO (пусто — не настроен), обновление, скрипт,
    переводы, логин, чат.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    REFRESH_PATH: ClassVar[str] = "/auth/refresh"
    JS_PATH: ClassVar[str] = "/auth/page.js"

    sso: str
    refresh: str
    js: str
    translations: str
    login: str
    app: str

    @classmethod
    def of(cls, url_prefix: str, sso_path: str) -> PageUrls:
        sso = ""
        if sso_path:
            sso = f"{url_prefix}{sso_path}"

        return cls(
            sso=sso,
            refresh=f"{url_prefix}{cls.REFRESH_PATH}",
            js=f"{url_prefix}{cls.JS_PATH}",
            translations=f"{url_prefix}/project/translations",
            login=f"{url_prefix}/login",
            app=f"{url_prefix}/",
        )


class SessionRefresh:
    """POST /auth/refresh и page.js: обновление входа без участия пользователя."""

    SCRIPT_PATH: ClassVar[str] = "public/page.js"
    """Собранный web/page (vite) внутри app_root chainlit."""

    def __init__(
        self,
        urls: PageUrls,
        auth: AuthService,
        sessions: ChainlitSessions,
        app_root: Path,
    ) -> None:
        self._urls = urls
        self._auth = auth
        self._sessions = sessions
        self._script_path = app_root / self.SCRIPT_PATH
        self._tokens = RequestTokens(auth.cookie().name)
        self._logger = logging.getLogger(SessionRefresh.__name__)

    @property
    def urls(self) -> PageUrls:
        return self._urls

    def install(self, chainlit_app: FastAPI) -> None:
        script = self.script()

        async def page_js() -> Response:
            return Response(content=script, media_type="application/javascript")

        self.prepend_route(
            chainlit_app, PageUrls.REFRESH_PATH, self.refresh, methods=["POST"]
        )
        # chainlit подставляет свой префикс к custom_js: роут скрипта — с полным адресом
        self.prepend_route(chainlit_app, self._urls.js, page_js)
        self._install_page_js()

    async def refresh(self, request: Request) -> Response:
        """204 + новая cookie; 401 Negotiate — браузер повторит сам; 403 — разлогин."""
        token = self._tokens.of_request(request)
        outcome = await self._auth.refresh_session(SsoRequests.of(request), token)
        if isinstance(outcome, SsoRefused):
            self._logger.info("session refresh refused: %s", outcome.reason)
            return Response(status_code=403)

        if isinstance(outcome, SsoChallenge):
            return SsoResponses.silent_challenge()

        return self._adopted(request, outcome)

    def _adopted(self, request: Request, session: IssuedSession) -> Response:
        response = Response(status_code=204)
        SessionCookie(self._auth.cookie()).put(response, request.cookies, session.token)
        identifier = session.signed.identifier
        adopted = self._sessions.adopt_token(identifier, session.token)
        self._logger.info(
            "sessions adopted the refreshed token [user=%s] [sessions=%d]",
            identifier,
            adopted,
        )

        return response

    def script(self) -> str:
        """page.js с подставленными адресами; сервер знает только URL.

        RuntimeError — сборки скрипта нет в app_root: страница без обновления входа
        и кнопки SSO — ошибка развёртывания, не режим работы.
        """
        if not self._script_path.is_file():
            msg = (
                "sign-in page script is not built: expected a file at "
                f"{self._script_path}"
            )
            raise RuntimeError(msg)

        template = self._script_path.read_text(encoding="utf-8")
        values = {
            PageJsVar.SSO_URL: self._urls.sso,
            PageJsVar.REFRESH_URL: self._urls.refresh,
            PageJsVar.LOGIN_URL: self._urls.login,
            PageJsVar.REFRESH_HEADER: OwnRequest.HEADER,
            PageJsVar.REFRESH_HEADER_VALUE: OwnRequest.VALUE,
            PageJsVar.TRANSLATIONS_URL: self._urls.translations,
        }
        for var, value in values.items():
            template = template.replace(var.value, value)

        return template

    def _install_page_js(self) -> None:
        """Подключает page.js через custom_js chainlit."""
        existing = cl_config.ui.custom_js
        if not existing:
            cl_config.ui.custom_js = self._urls.js
            return

        if existing == self._urls.js:
            return

        # слот один на приложение: занявший его скрипт обязан подгрузить page.js
        self._logger.info(
            "custom_js already set (%s) — expecting it to load %s itself",
            existing,
            self._urls.js,
        )

    @staticmethod
    def prepend_route(
        chainlit_app: FastAPI,
        path: str,
        endpoint: Callable[..., Awaitable[Any]],
        methods: Sequence[str] = ("GET",),
    ) -> None:
        """Добавляет роут в начало, иначе его перехватит chainlit."""
        chainlit_app.add_api_route(
            path, endpoint, methods=list(methods), include_in_schema=False
        )

        route = chainlit_app.router.routes.pop()
        chainlit_app.router.routes.insert(0, route)
