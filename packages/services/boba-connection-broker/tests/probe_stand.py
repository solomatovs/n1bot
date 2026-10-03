"""Пробный стенд параметров-соединений: выдуманные типы и инструменты.

Общий для тестов обвязок параметров-соединений: проверяется сама обвязка, а
не установленные пакеты соединений, поэтому типы здесь свои.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from datetime import timedelta
from typing import Annotated, Any, Literal
from uuid import uuid4

from langchain_core.messages import ToolCall, ToolMessage
from langchain_core.tools import BaseTool, StructuredTool
from pydantic import SecretStr, create_model

from boba.connection_broker.sealed import SealedConnectionParams
from boba.connection_broker.sealing import SealingToolServer, SentConnections
from boba.connection_broker.user_connections import ArmedConnections
from boba.connections.base import ClientIdentity, ConnectionBase
from boba.connections.manifest import ConnectionTypeManifest, ConnectionTypes
from boba.connections.sealed import SealKeys
from boba.connections.stored import GrantedConnection, StoredConnection
from boba.identity.context import Subject
from boba.stand_core.context import TEST_CONTEXTS, make_context
from boba.toolkit.calls import ToolCallBase
from boba.toolkit.facade import UserConnection, tool
from boba.toolkit.result import TableResult
from boba.toolrun.bridge import ToolBridge
from boba.toolrun.stream_calls import (
    LocalDagService,
    StreamGroupsConfig,
    ToolServer,
)

SECRET = "probe-secret-value"
LOGIN = "ivanov"


class ProbeConnection(ConnectionBase):
    """Профиль выдуманного типа: чтобы тест не зависел от установленных пакетов."""

    kind: Literal["probe"] = "probe"
    host: str
    password: SecretStr
    client: str = ""

    def trace(self) -> str:
        return f"auth=password host={self.host}"

    def labeled(self, client: ClientIdentity) -> ProbeConnection:
        return self.model_copy(update={"client": client.login})


class OtherConnection(ConnectionBase):
    """Второй тип: нужен, чтобы проверить выбор и сверку по виду соединения."""

    kind: Literal["other"] = "other"
    host: str

    def trace(self) -> str:
        return f"host={self.host}"


async def _probe(connection: ConnectionBase) -> str:
    return "ok"


TYPES = ConnectionTypes(
    {
        "probe": ConnectionTypeManifest(
            kind="probe", model=ProbeConnection, probe=_probe
        ),
        "other": ConnectionTypeManifest(
            kind="other", model=OtherConnection, probe=_probe
        ),
    }
)


class ProbeTools:
    """Инструменты с параметрами-соединениями и их вызов в контексте LOGIN."""

    def tool(self, name: str, fields: dict[str, Any]) -> BaseTool:
        """Инструмент, чьё тело возвращает полученные аргументы как есть."""
        schema = create_model(f"{name}_args", __base__=ToolCallBase, **fields)

        async def body(**kwargs: object) -> dict[str, object]:
            return kwargs

        return StructuredTool(
            name=name, description=name, args_schema=schema, coroutine=body
        )

    def one_connection(self) -> BaseTool:
        fields = {
            "connection": (Annotated[ProbeConnection, UserConnection], ...),
            "sql": (str, ...),
        }

        return self.tool("probe_query", fields)

    def two_connections(self) -> BaseTool:
        fields = {
            "source": (Annotated[ProbeConnection, UserConnection], ...),
            "target": (Annotated[ProbeConnection, UserConnection], ...),
        }

        return self.tool("probe_copy", fields)

    async def call(self, tool: BaseTool, args: dict[str, Any]) -> dict[str, Any]:
        with TEST_CONTEXTS.applied(make_context("t1", login=LOGIN, roles=("read",))):
            return await tool.ainvoke(args)


class Rows:
    """Хранилище в памяти на месте таблицы connections."""

    def __init__(self, rows: Sequence[StoredConnection]) -> None:
        self._rows = list(rows)

    async def for_subject(
        self, subject: Subject, kind: str
    ) -> Sequence[GrantedConnection]:
        """Как SubjectGrantsQuery: дубль — одно имя дважды внутри вида."""
        names: Counter[str] = Counter()
        for row in self._rows:
            if row.kind == kind:
                names[row.name] += 1

        found: list[GrantedConnection] = []
        for row in self._rows:
            if row.kind != kind:
                continue

            found.append(GrantedConnection(row=row, ambiguous=names[row.name] > 1))

        return found


class Credentials:
    """Источник кредов вызова: kerberos-секций у пробного типа нет."""

    async def for_connection(
        self, connection: ConnectionBase, credential: object
    ) -> ConnectionBase:
        return connection


class SealedStand:
    """Клиент и исполнитель в одном процессе: путь соединения целиком.

    Исполнитель — LocalDagService с пробными инструментами под обвязкой
    запечатанных соединений, объявляющий возможность с ключом; клиент —
    SealingToolServer поверх него с таблицей соединений в памяти. Вызовы
    идут через порт клиента в контексте LOGIN.
    """

    STREAM_CONFIG = StreamGroupsConfig(
        open_sec=60.0,
        stall_sec=120.0,
        poll_sec=0.2,
        pipe_bytes=65536,
        pipe_bytes_max=1 << 30,
    )

    def __init__(self, rows: Sequence[StoredConnection]) -> None:
        self.keys = SealKeys()
        self.params = SealedConnectionParams(self.keys, lambda: TYPES, TEST_CONTEXTS)
        self.sent = SentConnections()

        tools = [self._query_tool(), self._copy_tool()]
        self.params.bind_all(tools)

        store = Rows(rows)
        self.connections = ArmedConnections(
            lambda: store,  # type: ignore[arg-type]
            Credentials,  # type: ignore[arg-type]
            TEST_CONTEXTS,
        )
        self.executor = LocalDagService(tools, self.STREAM_CONFIG, (self.params,))
        self.client = SealingToolServer(
            self.executor,
            self.connections,
            self.sent,
            timedelta(minutes=10),
        )

    @staticmethod
    def row(name: str, connection: ConnectionBase) -> StoredConnection:
        return StoredConnection(id=uuid4(), name=name, connection=connection)

    @classmethod
    def probe_row(cls, name: str, host: str) -> StoredConnection:
        return cls.row(name, ProbeConnection(host=host, password=SecretStr(SECRET)))

    async def call(self, name: str, args: dict[str, Any]) -> ToolMessage:
        """Вызов через порт клиента, как его шлёт модель."""
        return await self.call_through(self.client, name, args)

    async def call_through(
        self, server: ToolServer, name: str, args: dict[str, Any]
    ) -> ToolMessage:
        """Вызов через порт server в контексте LOGIN."""
        call = ToolCall(
            name=name, args=args, id=f"call_{uuid4().hex}", type="tool_call"
        )
        with TEST_CONTEXTS.applied(make_context("t1", login=LOGIN, roles=("read",))):
            return await server.call(call)

    @staticmethod
    def _query_tool() -> BaseTool:
        @tool
        async def probe_query(
            connection: Annotated[ProbeConnection, UserConnection], sql: str
        ) -> TableResult:
            """Пробный запрос: возвращает полученный профиль строкой таблицы."""
            row = {
                "host": connection.host,
                "client": connection.client,
                "password": connection.password.get_secret_value(),
            }

            return TableResult(rows=[row])

        return ToolBridge.as_structured_tool(probe_query)

    @staticmethod
    def _copy_tool() -> BaseTool:
        @tool
        async def probe_copy(
            source: Annotated[ProbeConnection, UserConnection],
            target: Annotated[ProbeConnection, UserConnection],
        ) -> TableResult:
            """Пробная перекачка: возвращает хосты обоих профилей."""
            return TableResult(rows=[{"source": source.host, "target": target.host}])

        return ToolBridge.as_structured_tool(probe_copy)
