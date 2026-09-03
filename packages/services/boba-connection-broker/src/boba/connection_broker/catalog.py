"""Каталог соединений субъекта для модели: имя, вид и описание строки.

Инструмент connection_list — единственный способ, которым модель узнаёт, какие
соединения ей доступны. Он общий для всех видов: плагины своих списков не
держат. По виду модель понимает, какому инструменту имя годится, по описанию —
какое из них подходит задаче пользователя.

Ошибки:
ConnectionStoreError — таблица соединений недоступна.
RefusalError — вызов идёт вне сессии пользователя.
"""

from __future__ import annotations

from typing import Any, ClassVar

from langchain_core.tools import BaseTool, tool
from pydantic import BaseModel, ConfigDict

from boba.connection_broker.service import UserConnectionsService
from boba.identity.context import CallContext
from boba.toolkit.result import TableResult, ToolResult, pack_result

__all__ = ["ConnectionCatalogConfig", "build_connection_tools"]


class ConnectionCatalogConfig(BaseModel):
    """Секция [tool.connections]: своих параметров у инструмента нет."""

    model_config = ConfigDict(extra="ignore")


class CatalogColumn:
    """Колонки выдачи connection_list; их же читает модель в ответе."""

    NAME: ClassVar[str] = "connection"
    KIND: ClassVar[str] = "kind"
    DESCRIPTION: ClassVar[str] = "description"


class CatalogPrompt:
    """Описание инструмента для модели."""

    LIST: ClassVar[str] = (
        "Соединения, доступные пользователю: имя, вид (postgres, clickhouse, "
        "web, ...) и описание. Имя из этого списка передаётся инструментам в "
        "параметр соединения; вид говорит, какому инструменту имя подходит."
    )


class ConnectionCatalog:
    """Строки субъекта в таблицу выдачи."""

    def __init__(self, service: UserConnectionsService) -> None:
        self._service = service

    async def rows(self) -> TableResult:
        subject = CallContext.current().subject
        visible = await self._service.visible_all(subject)

        rows: list[dict[str, Any]] = []
        for item in visible.rows:
            rows.append(
                {
                    CatalogColumn.NAME: item.row.name,
                    CatalogColumn.KIND: item.row.kind,
                    CatalogColumn.DESCRIPTION: item.row.profile.description,
                }
            )

        rows.sort(key=lambda row: (row[CatalogColumn.KIND], row[CatalogColumn.NAME]))

        return TableResult(rows=rows, note=self._note(len(rows)))

    @staticmethod
    def _note(count: int) -> str | None:
        if count:
            return None

        return "no connections are granted to you"


def build_connection_tools(
    cfg: ConnectionCatalogConfig, service: UserConnectionsService
) -> list[BaseTool]:
    """Инструмент connection_list для реестра приложения."""
    catalog = ConnectionCatalog(service)

    @tool(response_format="content_and_artifact")
    async def connection_list() -> tuple[str, ToolResult]:
        """Доступные соединения: имя, вид и описание."""
        return pack_result(await catalog.rows())

    connection_list.description = CatalogPrompt.LIST

    return [connection_list]
