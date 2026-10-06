"""Инструменты connection_list и connection_search: соединения, выданные субъекту.

Так модель узнаёт доступные ей соединения: имя, вид, хост и описание.
connection_list отдаёт все, connection_search — отобранные фильтрами по
колонкам. Инструменты принадлежат процессу, который хранит соединения: их
тела исполняются в нём самом и читают ConnectionStore — тот же граф грантов
и то же правило дублей, что у выбора строки под вызов. Песочница и DAG им
не нужны. Секреты профилей не читаются: в выдаче только открытые поля.

Ошибки:
ConnectionStoreError — база отказала в запросе к таблицам соединений.
RefusalError — вызов идёт вне контекста CallContext.
"""

from __future__ import annotations

from typing import Annotated, Any, ClassVar

from pydantic import Field

from boba.access.grants import ConnectionFilter
from boba.connection_broker.user_connections import StoreRef
from boba.identity.context import CallContexts, Subject
from boba.toolkit.facade import PayloadTool, tool
from boba.toolkit.result import TableResult
from boba.toolrun.hosted import HostedTool, ToolHosting

__all__ = ["ConnectionTools", "GrantedConnections"]


class GrantedConnections:
    """Каталог соединений субъекта для модели: записи ConnectionStore.catalog
    строками таблицы. Создаёт его ConnectionTools; хранилище
    приходит ссылкой и берётся на каждый запрос."""

    EMPTY_NOTE: ClassVar[str] = "no connections are granted to you"
    NO_MATCH_NOTE: ClassVar[str] = "no granted connections match the filters"

    def __init__(self, store_ref: StoreRef) -> None:
        self._store_ref = store_ref

    async def rows(self, subject: Subject) -> TableResult:
        """Все соединения субъекта."""
        return await self.search(subject, ConnectionFilter.none())

    async def search(self, subject: Subject, flt: ConnectionFilter) -> TableResult:
        """Соединения субъекта, прошедшие фильтры."""
        rows: list[dict[str, Any]] = []
        for entry in await self._store_ref().catalog(subject, flt):
            rows.append(entry.model_dump())

        return TableResult(rows=rows, note=self._note(len(rows), flt))

    @classmethod
    def _note(cls, count: int, flt: ConnectionFilter) -> str | None:
        if count:
            return None

        if flt.empty:
            return cls.EMPTY_NOTE

        return cls.NO_MATCH_NOTE


class ConnectionTools:
    """Инструменты каталога соединений для модели.

    Соединения и гранты на них хранит чат, поэтому эти инструменты — его
    собственные: чат собирает их здесь и отдаёт загрузчику инструментов
    рядом с плагинами (ChatPlugins) как свой сервер инструментов. Субъект
    вызова берётся из контекста вызова.
    """

    def __init__(self, store_ref: StoreRef, contexts: CallContexts) -> None:
        self._catalog = GrantedConnections(store_ref)
        self._contexts = contexts
        self._hosting = ToolHosting()

    def build(self) -> list[HostedTool]:
        """Инструменты connection_list и connection_search."""
        return [
            self._hosting.hosted(self._list_tool()),
            self._hosting.hosted(self._search_tool()),
        ]

    def _list_tool(self) -> PayloadTool:
        catalog = self._catalog
        contexts = self._contexts

        @tool
        async def connection_list() -> TableResult:
            """Все соединения, доступные пользователю: ссылка, вид (postgres,
            clickhouse, web, ...), хост и описание. Ссылка из колонки connection
            передаётся инструментам в параметр соединения как есть; вид говорит,
            какому инструменту соединение подходит."""
            return await catalog.rows(contexts.subject())

        return connection_list

    def _search_tool(self) -> PayloadTool:
        catalog = self._catalog
        contexts = self._contexts

        @tool
        async def connection_search(
            kind: Annotated[
                str,
                Field(
                    description=(
                        "Вид соединения, точное совпадение: postgres, clickhouse, "
                        "web. Пусто — любой вид."
                    ),
                ),
            ] = "",
            name: Annotated[
                str,
                Field(
                    description=(
                        "Подстрока имени соединения без учёта регистра. Пусто — "
                        "любое имя."
                    ),
                ),
            ] = "",
            host: Annotated[
                str,
                Field(
                    description=(
                        "Подстрока хоста сервера без учёта регистра. Пусто — "
                        "любой хост."
                    ),
                ),
            ] = "",
            description: Annotated[
                str,
                Field(
                    description=(
                        "Слова через пробел; каждое должно встретиться в описании "
                        "соединения без учёта регистра. Пусто — любое описание."
                    ),
                ),
            ] = "",
        ) -> TableResult:
            """Найти соединения, доступные пользователю, фильтрами по колонкам:
            фильтры складываются по И, пустой фильтр не применяется. Возвращает
            connection, kind, host, description; ссылка из колонки connection
            передаётся инструментам в параметр соединения как есть, вид говорит,
            какому инструменту соединение подходит."""
            flt = ConnectionFilter(
                kind=kind, name=name, host=host, description=description
            )

            return await catalog.search(contexts.subject(), flt)

        return connection_search
