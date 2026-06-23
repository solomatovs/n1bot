import logging

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from boba.chainlit2.errors.model import BaseError, to_domain


class DomainErrorMiddleware:
    """
    Единая точка обработки исключений приложения.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self._encoding = "utf-8"
        self._encoding_error = "ignore"
        self._logger = logging.getLogger(__name__)

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
            if not isinstance(e, BaseError):
                self._logger.exception(str(e))
            else:
                self._logger.error(str(e))

            domain = to_domain(e)

            if started:
                # ответ уже начат — подменить его уже нельзя
                raise

            if (http := domain.http_message()) is not None:
                # JSON {"detail": <code>} - для chainlut это важно
                # он мапит code в auth.login.errors.<code>
                # поэтому можно использовать локализации для отображения ошибок
                await JSONResponse(
                    content={"detail": http.content},
                    status_code=http.status_code,
                    headers=http.headers,
                )(scope, receive, send)
