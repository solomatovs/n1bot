"""Отсев действий фронта, адресованных закрытой websocket-сессии.
from boba.chainlit.infra.session import session_source_ref

Ошибки: своих не выпускает; запрос с живой сессией уходит дальше как есть.
"""

import json
import logging

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from boba.chainlit.infra.session import session_source_ref

__all__ = ["StaleActionMiddleware"]


class ActionRef(BaseModel):
    """Действие в теле запроса: для лога нужно только имя."""

    model_config = ConfigDict(extra="ignore")

    name: str = ""


class ActionEnvelope(BaseModel):
    """Тело POST /project/action: сессия, которой адресовано действие."""

    model_config = ConfigDict(extra="ignore")

    session_id: str = Field(alias="sessionId", min_length=1)
    action: ActionRef = ActionRef()


class StaleActionMiddleware:
    """Действие по закрытой сессии: гонка ухода со страницы, а не сбой.

    Фронт шлёт действие http-запросом, а исполняет его chainlit в контексте
    websocket-сессии. Вкладку закрыли или перезагрузили — сокет умирает
    раньше, чем долетает последний запрос, и chainlit падает ValueError ещё
    до колбэка действия. Предусловие проверяется здесь: мёртвая сессия —
    это 409 и строка в журнале, а не внутренняя ошибка с трассировкой.

    Слежение панели такой запрос и не требуется снимать: вотчер сам гаснет,
    когда у треда не остаётся живых сокетов.
    """

    PATH = "/project/action"
    """Хвост маршрута действий chainlit: приложение примонтировано с префиксом."""

    STATUS = 409
    """Сессии больше нет — повторять запрос бессмысленно, это не ошибка сервера."""

    def __init__(self, app: ASGIApp) -> None:
        self._app = app
        self._logger = logging.getLogger(__name__)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            return await self._app(scope, receive, send)

        if scope.get("method") != "POST":
            return await self._app(scope, receive, send)

        if not str(scope.get("path", "")).endswith(self.PATH):
            return await self._app(scope, receive, send)

        body = await self._body(receive)
        envelope = self._envelope(body)

        if envelope is None:
            return await self._app(scope, self._replay(body), send)

        if session_source_ref().by_id(envelope.session_id).present:
            return await self._app(scope, self._replay(body), send)

        action_name = envelope.action.name
        if not action_name:
            action_name = "?"

        detail = f"action {action_name} dropped: session {envelope.session_id} is gone"
        self._logger.info("%s", detail)
        response = JSONResponse(content={"detail": detail}, status_code=self.STATUS)
        return await response(scope, self._replay(body), send)

    @staticmethod
    def _envelope(body: bytes) -> ActionEnvelope | None:
        """Разбор тела; None — тело не по контракту, судить о нём не нам."""
        try:
            raw = json.loads(body)
        except ValueError:
            return None

        try:
            return ActionEnvelope.model_validate(raw)
        except ValidationError:
            return None

    @staticmethod
    async def _body(receive: Receive) -> bytes:
        """Тело запроса целиком: у действия оно маленькое, буфер безопасен."""
        collected = bytearray()

        while True:
            message = await receive()
            if message["type"] != "http.request":
                break

            collected.extend(message.get("body", b""))
            if not message.get("more_body", False):
                break

        return bytes(collected)

    @staticmethod
    def _replay(body: bytes) -> Receive:
        """Прочитанное тело для следующего слоя: читать сокет второй раз нельзя."""
        sent = False

        async def receive() -> Message:
            nonlocal sent
            if sent:
                return {"type": "http.disconnect"}

            sent = True
            return {"type": "http.request", "body": body, "more_body": False}

        return receive
