"""HTTP-граница ошибок: BaseError -> статус и тело ответа, остальное -> 500."""

import logging

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from boba.identity.errors import BaseError, FailureReport, to_domain
from boba.identity.session import LogLine

__all__ = ["DomainErrorMiddleware"]


class DomainErrorMiddleware:
    "Единая точка обработки исключений приложения"

    def __init__(self, app: ASGIApp) -> None:
        self.app = app
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
            # http-слой пишет в журнал ту же формулировку, что чат и история
            described = FailureReport.of(e).log
            if not isinstance(e, BaseError):
                self._logger.exception("%s", LogLine.safe(described))
            else:
                self._logger.error("%s", LogLine.safe(described))

            domain = to_domain(e)

            if started:
                # ответ уже начат — подменить его уже нельзя
                raise

            if (http := domain.http_message()) is not None:
                # chainlit мапит detail в auth.login.errors.<code> (локализация)
                await JSONResponse(
                    content={"detail": http.content},
                    status_code=http.status_code,
                    headers=http.headers,
                )(scope, receive, send)
