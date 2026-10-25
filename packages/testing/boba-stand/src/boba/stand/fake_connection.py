"""Фейковый модуль инструментов с параметром-соединением.

Запускается настоящим subprocess'ом, как fake_toolmod. Тело получает профиль
соединения postgres и возвращает, что увидело: тесты проверяют, что
соединение, названное ссылкой и запечатанное клиентом, доезжает до тела
профилем с подписью вызывающего.
"""

from __future__ import annotations

from typing import Annotated, Final

from boba.db.postgres.connection import PostgresConfig
from boba.toolkit.entry import ToolMain
from boba.toolkit.facade import UserConnection, tool
from boba.toolkit.manifest import ToolPlugin
from boba.toolkit.result import MarkdownResult


@tool
async def fake_connection_host(
    connection: Annotated[PostgresConfig, UserConnection],
) -> MarkdownResult:
    """Называет хост и базу соединения, с которым его вызвали."""
    return MarkdownResult(text=f"{connection.host}|{connection.dbname}")


TOOLS: Final = ToolMain.toolset(fake_connection_host)

PLUGIN: Final = ToolPlugin(id="fake_connection", tools=TOOLS)
"""Объявление плагина модуля: тесты зовут его программой плагина
`python -m boba.sandbox.program --declared <модуль>:PLUGIN`."""
