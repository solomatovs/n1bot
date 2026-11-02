"""HTTP-адаптеры SPNEGO для маршрутов OAuth и чата.

Запрос Starlette становится моделью SsoRequest; ответы сообщают браузеру,
что нужен билет Kerberos, и при необходимости ведут на страницу входа.
"""

import html
from typing import ClassVar

from starlette.requests import Request
from starlette.responses import Response

from boba.identity.sso import OwnRequest, RequestHeader, SsoErrorCode, SsoRequest

__all__ = ["SsoRequests", "SsoResponses"]


class SsoRequests:
    """Запрос SPNEGO-обмена из запроса starlette: заголовки и адрес клиента.
    Создаётся маршрутом входа приложения и лежит у него полем."""

    def of(self, request: Request) -> SsoRequest:
        mark = request.headers.get(OwnRequest.HEADER, "")

        return SsoRequest(
            authorization=request.headers.get(RequestHeader.AUTHORIZATION, ""),
            own_request=OwnRequest.asked(mark),
            client=self.client_of(request),
        )

    def client_of(self, request: Request) -> str:
        """Лучший идентификатор клиента: реальный IP за прокси, иначе peer.

        X-Forwarded-For и X-Real-IP доверяются, потому что nginx перед
        приложением перезаписывает их сам.
        """
        if xff := request.headers.get(RequestHeader.FORWARDED_FOR):
            first, _, _ = xff.partition(",")
            return first.strip()

        if real := request.headers.get(RequestHeader.REAL_IP):
            return real

        if request.client is not None:
            return request.client.host

        return SsoRequest.UNKNOWN_CLIENT


class SsoResponses:
    """Ответы SPNEGO-обмена: 401 Negotiate — браузер домена повторит запрос сам.
    Создаётся маршрутом входа kerberos приложения и лежит у него полем."""

    NEGOTIATE: ClassVar[str] = "Negotiate"
    PAGE: ClassVar[str] = (
        '<!doctype html><meta http-equiv="refresh" content="0;url={url}">'
    )

    def headers(self) -> dict[str, str]:
        return {RequestHeader.WWW_AUTHENTICATE.value: self.NEGOTIATE}

    def challenge(self, login_url: str) -> Response:
        """401 со страницей-переходом: без тикета браузер уйдёт на логин с кодом."""
        url = html.escape(SsoErrorCode.TICKET.login_url(login_url), quote=True)

        return Response(
            content=self.PAGE.format(url=url),
            status_code=401,
            headers=self.headers(),
            media_type="text/html",
        )

    def silent_challenge(self) -> Response:
        """401 без страницы: ответ читает скрипт обновления, не человек."""
        return Response(status_code=401, headers=self.headers())
