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

from enum import StrEnum

__all__ = ["McpAction", "McpAppError", "McpOrigin"]


class McpAppError(Exception):
    """Операцию приложения не исполнить; текст — для пользователя."""


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


class McpAuthChoice(StrEnum):
    """Способы входа, доступные в пользовательской форме."""

    NONE = "none"
    BEARER = "bearer"


class ReservedHeader(StrEnum):
    """Заголовки, которыми управляет транспорт MCP."""

    AUTHORIZATION = "Authorization"
    SESSION = "Mcp-Session-Id"
    VERSION = "Mcp-Protocol-Version"
    CONTENT_TYPE = "Content-Type"
    ACCEPT = "Accept"
