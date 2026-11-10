"""Приложение MCP-серверов чата: команда /mcp и действия приложения.

Слои пакета:
commands (agent/commands.py) — команда /mcp вкладки: возвращает итог
    CommandElement с элементом McpServers в историю.
store — McpServerStore: таблица mcp_servers и выдачи в grants.
sources — ServerSource: набор серверов зрителя и их конфиги; единственное
    место, через которое aid узнаёт о серверах и их правах.
directory — McpDirectory: операции приложения над McpServers; единственное
    место логики, модели входа и ответа — проводной формат.
actions (infra/callback.py) — обработчики McpAction: разбирают payload,
    собирают McpViewer и зовут McpDirectory.

Ошибки слоя:
McpAppError — действие приложения не исполнить: сервер не выдан зрителю
    (чужая выдача не назвать), сервер недоступен, список или состояние не
    прочитаны; текст — для пользователя приложения.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from enum import StrEnum

from boba.identity.errors import BaseError, HttpErrorMessage, ViewErrorMessage

__all__ = ["McpAction", "McpAppError", "McpAppErrors", "McpOrigin"]


class McpAppError(BaseError):
    """Ошибка операции MCP: диагностика для журнала и сообщение для UI и истории."""

    def __init__(self, message: str, *, user_message: str | None = None) -> None:
        super().__init__(message)
        if user_message is None:
            user_message = message

        self._user_message = user_message

    def view_message(self) -> ViewErrorMessage:
        return ViewErrorMessage(self._user_message)

    def history_message(self) -> str:
        return self._user_message

    def http_message(self) -> HttpErrorMessage:
        return HttpErrorMessage(status_code=400, content=self._user_message)


class McpAppErrors:
    """Переводит сбои зависимостей формы, каталога и источника в McpAppError."""

    @contextmanager
    def boundary(self, action: str) -> Iterator[None]:
        try:
            yield
        except McpAppError:
            raise
        except Exception as exc:
            raise McpAppError(
                f"{action} failed: {exc}",
                user_message=f"{action} failed. Details are in the AID log.",
            ) from exc


class McpOrigin(StrEnum):
    """Откуда сервер в приложении зрителя."""

    USER = "user"
    """Сервер выдан лично: владелец его правит и удаляет."""
    ROLE = "role"
    """Сервер выдан роли: виден только на чтение."""


class McpAction(StrEnum):
    """Действия приложения «элемент → сервер»: callAction на фронте."""

    ADD = "mcp_add"
    UPDATE = "mcp_update"
    REMOVE = "mcp_remove"
    TOGGLE = "mcp_toggle"
    FORM = "mcp_form"
    LIST = "mcp_list"
    STATUS = "mcp_status"
    DETAIL = "mcp_detail"
    RECONNECT = "mcp_reconnect"
    OAUTH_START = "mcp_oauth_start"
    OAUTH_DISCONNECT = "mcp_oauth_disconnect"


class McpAuthChoice(StrEnum):
    """Способы входа, доступные в пользовательской форме."""

    NONE = "none"
    BEARER = "bearer"
    OAUTH = "oauth"


class ReservedHeader(StrEnum):
    """Заголовки, которыми управляет транспорт MCP."""

    AUTHORIZATION = "Authorization"
    SESSION = "Mcp-Session-Id"
    VERSION = "Mcp-Protocol-Version"
    CONTENT_TYPE = "Content-Type"
    ACCEPT = "Accept"
