"""Страницы входа OAuth и подтверждение шага входа браузером."""

from __future__ import annotations

import html
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from importlib.resources import files
from typing import ClassVar
from urllib.parse import urlencode, urlsplit

from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response
from starlette.routing import Route

from boba.auth.transport.http import SsoRequests, SsoResponses
from boba.identity.errors import AuthenticationError, AuthorizationError
from boba.identity.sso import SsoChallenge
from boba.oauth.forms import LoginField
from boba.oauth.server.codes import PendingLogin
from boba.oauth.server.provider import AuthServer
from boba.oauth.server.sealed import SealedValues


@dataclass(frozen=True)
class LoginVisit:
    """Заход браузера на страницу начатого входа: какой вход открыт и какое
    подтверждение страницы браузер прислал в cookie (пусто — не прислал).

    Создаётся страницами входа (LoginPages) на каждый запрос. Подтверждение
    выдаётся странице входа и лежит в двух местах: в cookie браузера и в
    самой странице (поле формы, ссылка kerberos). Шаг входа принимается,
    только если оба совпали, — так вход нельзя завершить ссылкой или формой
    с чужого сайта, минуя страницу с названием приложения и адресом возврата.
    """

    txn: str
    held: PendingLogin
    cookie: str

    def confirmed(self, proof: str) -> bool:
        if not self.cookie:
            return False

        if not proof:
            return False

        return secrets.compare_digest(
            self.cookie.encode(SealedValues.ENCODING),
            proof.encode(SealedValues.ENCODING),
        )


class LoginTemplates:
    """HTML и CSS страниц входа из данных пакета boba-oauth."""

    def __init__(self) -> None:
        resources = files("boba.oauth").joinpath("templates")
        self.page = resources.joinpath("login.html").read_text(encoding="utf-8")
        self.client = resources.joinpath("client.html").read_text(encoding="utf-8")
        self.form = resources.joinpath("form.html").read_text(encoding="utf-8")
        self.sso = resources.joinpath("sso.html").read_text(encoding="utf-8")
        self.error = resources.joinpath("error.html").read_text(encoding="utf-8")
        self.css = resources.joinpath("login.css").read_text(encoding="utf-8")


class LoginPages:
    """Страницы входа человека на шаге authorize: форма логина и пароля
    (local, ldap) и обмен SPNEGO (kerberos).

    Создаётся сборкой процесса над сервером авторизации; routes() ставятся
    в корень приложения рядом с его маршрутами. Удачный вход завершает
    начатый вход сервера и ведёт браузер на адрес возврата клиента с кодом.
    Страница называет службу (service), приложение, начавшее вход, и адрес
    возврата; шаг входа без подтверждения этой страницы (LoginVisit) не
    принимается.
    """

    UNNAMED: ClassVar[str] = "without a name"
    NO_TICKET: ClassVar[str] = (
        "kerberos sign-in needs a ticket of your domain session: the browser sent none"
    )
    NO_PROVIDERS: ClassVar[str] = (
        "no sign-in method for people is configured: [auth] of the service "
        "has no local, ldap or kerberos entry"
    )
    UNCONFIRMED: ClassVar[str] = (
        "this sign-in step did not come from the sign-in page of this browser: "
        "check the application below and sign in on this page"
    )
    HEADERS: ClassVar[Mapping[str, str]] = {
        "Cache-Control": "no-store",
        "Content-Security-Policy": "frame-ancestors 'none'",
        "X-Frame-Options": "DENY",
    }

    def __init__(self, server: AuthServer, public_url: str, service: str) -> None:
        self._server = server
        self._service = html.escape(service)
        self._sign_ins = server.sign_ins()
        self._public_url = public_url.rstrip("/")
        public = urlsplit(self._public_url)
        self._cookie_path = f"{public.path}{AuthServer.LOGIN_PATH}"
        self._cookie_secure = public.scheme == "https"
        self._requests = SsoRequests()
        self._responses = SsoResponses()
        self._templates = LoginTemplates()

    def routes(self) -> list[Route]:
        routes = [
            Route(AuthServer.LOGIN_PATH, self.form, methods=["GET"]),
            Route(AuthServer.LOGIN_PATH, self.submit, methods=["POST"]),
        ]
        if self._sign_ins.has_sso():
            routes.append(Route(LoginField.SSO.value, self.sso, methods=["GET"]))

        return routes

    async def form(self, request: Request) -> Response:
        visit = self._visit(request, request.query_params.get(LoginField.TXN.value, ""))
        if visit is None:
            return self._expired()

        return await self._page(visit, "", 200)

    async def submit(self, request: Request) -> Response:
        fields = await request.form()
        visit = self._visit(request, str(fields.get(LoginField.TXN.value, "")))
        if visit is None:
            return self._expired()

        if not visit.confirmed(str(fields.get(LoginField.PROOF.value, ""))):
            return await self._page(visit, self.UNCONFIRMED, 403)

        login = str(fields.get(LoginField.LOGIN.value, ""))
        password = str(fields.get(LoginField.PASSWORD.value, ""))
        try:
            signed = await self._sign_ins.by_password(login, password)
        except AuthenticationError as exc:
            return await self._page(visit, str(exc), 401)
        except AuthorizationError as exc:
            return await self._page(visit, str(exc), 403)

        return RedirectResponse(
            self._server.signed_in(visit.held, signed), status_code=302
        )

    async def sso(self, request: Request) -> Response:
        query = request.query_params
        visit = self._visit(request, query.get(LoginField.TXN.value, ""))
        if visit is None:
            return self._expired()

        if not visit.confirmed(query.get(LoginField.PROOF.value, "")):
            return await self._page(visit, self.UNCONFIRMED, 403)

        try:
            outcome = await self._sign_ins.exchange().handshake(
                self._requests.of(request)
            )
        except AuthorizationError as exc:
            return await self._page(visit, str(exc), 403)

        if isinstance(outcome, SsoChallenge):
            # браузер домена повторит запрос с билетом сам; остальным страница
            # входа остаётся с причиной и формой
            return await self._page(
                visit, self.NO_TICKET, 401, self._responses.headers()
            )

        return RedirectResponse(
            self._server.signed_in(visit.held, outcome.signed), status_code=302
        )

    def _visit(self, request: Request, txn: str) -> LoginVisit | None:
        """Заход на страницу начатого входа; None — вход не начат или истёк."""
        held = self._server.pending(txn)
        if held is None:
            return None

        cookie = request.cookies.get(LoginField.COOKIE.value, "")

        return LoginVisit(txn=txn, held=held, cookie=cookie)

    async def _page(
        self,
        visit: LoginVisit,
        error: str,
        status: int,
        headers: Mapping[str, str] | None = None,
    ) -> Response:
        providers = self._sign_ins.providers()
        if not error and not providers.password and not providers.sso:
            error = self.NO_PROVIDERS

        shown = ""
        if error:
            shown = self._templates.error.format(text=html.escape(error))

        proof = visit.cookie
        if not proof:
            proof = secrets.token_urlsafe(32)

        form = ""
        if providers.password:
            form = self._templates.form.format(
                action=html.escape(f"{self._public_url}{AuthServer.LOGIN_PATH}"),
                txn=html.escape(visit.txn),
                proof=html.escape(proof),
            )

        sso = ""
        if providers.sso:
            query = urlencode(
                {AuthServer.TXN: visit.txn, LoginField.PROOF.value: proof}
            )
            url = f"{self._public_url}{LoginField.SSO.value}?{query}"
            sso = self._templates.sso.format(
                url=html.escape(url), label="Sign in with Kerberos"
            )

        sent = dict(self.HEADERS)
        if headers is not None:
            sent.update(headers)

        response = HTMLResponse(
            self._templates.page.format(
                service=self._service,
                css=self._templates.css,
                error=shown,
                client=await self._client(visit.held),
                form=form,
                sso=sso,
            ),
            status_code=status,
            headers=sent,
        )
        response.set_cookie(
            LoginField.COOKIE.value,
            proof,
            max_age=AuthServer.PENDING_SEC,
            path=self._cookie_path,
            secure=self._cookie_secure,
            httponly=True,
            samesite="strict",
        )

        return response

    async def _client(self, held: PendingLogin) -> str:
        """Кто просит вход и куда вернётся браузер: по этой строке человек
        решает, его ли это вход."""
        name: str | None = None
        client = await self._server.get_client(held.client_id)
        if client is not None:
            name = client.client_name

        if not name:
            name = self.UNNAMED

        return self._templates.client.format(
            service=self._service,
            name=html.escape(name),
            redirect=html.escape(str(held.params.redirect_uri)),
        )

    def _expired(self) -> Response:
        text = (
            "the sign-in is not started or has expired: start it again from the client"
        )

        return HTMLResponse(
            self._templates.page.format(
                service=self._service,
                css=self._templates.css,
                error=self._templates.error.format(text=text),
                client="",
                form="",
                sso="",
            ),
            status_code=400,
            headers=self.HEADERS,
        )
