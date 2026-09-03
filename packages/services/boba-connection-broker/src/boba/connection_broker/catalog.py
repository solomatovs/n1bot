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

from collections.abc import Iterator, Sequence
from typing import Any, ClassVar

from langchain_core.tools import BaseTool, tool
from pydantic import BaseModel, ConfigDict

from boba.connection_broker.service import UserConnectionsService, VisibleConnection
from boba.connections.profile import StoredConnection
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
        for row in self._unambiguous(visible.rows):
            rows.append(
                {
                    CatalogColumn.NAME: row.name,
                    CatalogColumn.KIND: row.kind,
                    CatalogColumn.DESCRIPTION: row.profile.description,
                }
            )

        rows.sort(key=lambda row: (row[CatalogColumn.KIND], row[CatalogColumn.NAME]))

        return TableResult(rows=rows, note=self._note(len(rows)))

    @staticmethod
    def _unambiguous(
        visible: Sequence[VisibleConnection],
    ) -> Iterator[StoredConnection]:
        """Строки, чьё имя внутри своего вида выдано субъекту один раз.

        Имя-дубль вызов всё равно отвергнет как неоднозначное, поэтому модели
        его не показываем: выбрать из двух одинаковых строк нечего.
        """
        groups: dict[tuple[str, str], list[StoredConnection]] = {}
        for item in visible:
            groups.setdefault((item.row.kind, item.row.name), []).append(item.row)

        for group in groups.values():
            if len(group) != 1:
                continue

            yield group[0]

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
