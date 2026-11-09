"""Узкая совместимость HTTP SDK rc3 с повторным initialize ACP V2.

SDK ведёт очереди, SSE, сессии и закрытие соединения. Повтор initialize
использует прежний транспорт и возвращает ответ в POST с прежним ID.
Закрытые поля SDK нужны до появления поддержки повторов в самом SDK.
"""

from __future__ import annotations

import asyncio
from typing import Any

from starlette.responses import JSONResponse, Response

from acp.exceptions import RequestError
from acp.http.client import _HttpStreamTransport
from acp.http.protocol import (
    CONNECTION_ID_HEADER,
    is_initialize_request,
    message_id_key,
)
from acp.http.server import INITIALIZE_TIMEOUT_SECONDS, AcpServer


class HttpStream(_HttpStreamTransport):
    """Повтор шлёт Acp-Connection-Id и не открывает второй поток SSE."""

    async def _send_initialize(self, message: dict[str, Any]) -> None:
        if self._connection_id is None:
            await super()._send_initialize(message)
        else:
            await self._send_post(message)


class HttpServer(AcpServer):
    """Повтор инициализации существующего контекста вместо нового контекста rc3."""

    async def handle_post(
        self,
        message: Any,
        *,
        content_type: str | None,
        connection_id: str | None,
        session_id: str | None,
    ) -> Response:
        repeating = (
            connection_id is not None
            and isinstance(message, dict)
            and is_initialize_request(message)
            and content_type is not None
            and content_type.lower().startswith("application/json")
        )
        if not repeating or connection_id is None:
            return await super().handle_post(
                message,
                content_type=content_type,
                connection_id=connection_id,
                session_id=session_id,
            )

        connection = self._connections.get(connection_id)
        if connection is None:
            return JSONResponse({"error": "Unknown connection id"}, status_code=404)
        _, transport = connection
        headers = {CONNECTION_ID_HEADER: connection_id}
        if not transport.initialize_response.done():
            error = RequestError.invalid_request(
                {"details": "Wait for the previous initialize response"}
            )
            return JSONResponse(
                {"jsonrpc": "2.0", "id": message["id"], "error": error.to_error_obj()},
                headers=headers,
            )

        transport._initialize_id = message_id_key(message["id"])
        transport.initialize_response = asyncio.get_running_loop().create_future()
        try:
            await transport.deliver_to_agent(message)
            response = await asyncio.wait_for(
                transport.initialize_response, timeout=INITIALIZE_TIMEOUT_SECONDS
            )
        except TimeoutError:
            await self.handle_delete(connection_id=connection_id)
            return JSONResponse({"error": "initialize timed out"}, status_code=504)
        except BaseException:
            await self.handle_delete(connection_id=connection_id)
            raise
        return JSONResponse(response, headers=headers)
