import logging

from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

from .model import BaseError, HttpErrorMessage, InternalServiceError


def to_domain(e: Exception) -> BaseError:
    "Заворачивает любое НЕ доменное исключение в InternalServiceError"
    if isinstance(e, BaseError):
        return e

    return InternalServiceError(
        internal_detail=str(e),
    )


def render_http(status_code: int, content: str) -> Response:
    "Делает HTTP-ответ из BaseError"
    return JSONResponse({"error": content}, status_code=status_code)


class DomainErrorMiddleware:
    """
    Ловит любое необработанное исключение в своём app
    рендерит HTTP ответ с соответствующим исключением
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self._encoding = "utf-8"
        self._encoding_error = "ignore"
        self.logger = logging.getLogger("domain_error_middleware")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        started = False

        async def guarded_send(message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True

            await send(message)

        try:
            await self.app(scope, receive, guarded_send)
        except Exception as e:
            domain = to_domain(e)

            # логируем ошибку
            domain.log_message(self.logger)

            if started:
                # ответ уже начат
                raise

            # если есть пользовательское сообщение, то отправляем его
            if m := domain.http_message():
                await self._http_send(send, m)

    async def _http_send(self, send: Send, http: HttpErrorMessage) -> None:

        body = http.content.encode(encoding=self._encoding, errors=self._encoding_error)
        body_len = len(body)
        headers = []

        if body_len > 0:
            http.headers.extend(
                [
                    (
                        b"content-type",
                        f"text/plain; charset={self._encoding}".encode(
                            encoding=self._encoding, errors=self._encoding_error
                        ),
                    ),
                    (
                        b"content-length",
                        str(body_len).encode(
                            encoding=self._encoding, errors=self._encoding_error
                        ),
                    ),
                ]
            )
        # начинает отправку http response status
        await send(
            {
                "type": "http.response.start",
                "status": http.status_code,
                "headers": headers,
            }
        )
        # начинает отправку http response body
        await send({
            "type": "http.response.body",
            "body": body,
        })
