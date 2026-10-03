"""Пробный стенд параметров-соединений: выдуманные типы и инструменты.

Общий для тестов обвязок параметров-соединений: проверяется сама обвязка, а
не установленные пакеты соединений, поэтому типы здесь свои.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from langchain_core.tools import BaseTool, StructuredTool
from pydantic import SecretStr, create_model

from boba.connections.base import ClientIdentity, ConnectionBase
from boba.connections.manifest import ConnectionTypeManifest, ConnectionTypes
from boba.identity.context import CallContext
from boba.stand_core.context import make_context
from boba.toolkit.calls import ToolCallBase
from boba.toolkit.facade import UserConnection

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
        token = CallContext.push(make_context("t1", login=LOGIN, roles=("read",)))
        try:
            return await tool.ainvoke(args)
        finally:
            CallContext.pop(token)
