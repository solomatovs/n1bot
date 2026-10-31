"""Приложение MCP-серверов чата: команда /mcp и действия приложения.

Слои пакета:
commands (agent/commands.py) — команда /mcp вкладки: возвращает итог
    CommandElement с элементом McpServers в историю.
directory — McpDirectory: операции приложения над McpServers; единственное
    место логики, модели входа и ответа — проводной формат.
actions (infra/callback.py) — обработчики McpAction: разбирают payload,
    собирают McpViewer и зовут McpDirectory.

Ошибки слоя:
McpAppError — действие приложения не исполнить: сервер не из набора
    зрителя (чужой или админский не назвать), сервер недоступен, список или
    состояние не прочитаны; текст — для пользователя приложения.
"""

from __future__ import annotations

from enum import StrEnum

__all__ = ["McpAction"]


class McpAction(StrEnum):
    """Действия приложения «элемент → сервер»: callAction на фронте."""

    LIST = "mcp_list"
    STATUS = "mcp_status"
    DETAIL = "mcp_detail"
    RECONNECT = "mcp_reconnect"
