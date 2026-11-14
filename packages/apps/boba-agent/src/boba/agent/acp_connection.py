"""Сборка ACP-подключения: SDK выбирает независимый обработчик v1 либо v2."""

from __future__ import annotations

from acp import schema as v1_schema
from acp.agent.connection import AgentSideConnection as V1Connection
from acp.experimental.negotiation import AgentProtocolConnection, AgentProtocolRouter
from acp.experimental.v2.agent import AgentSideConnection as V2Connection
from acp.router import MessageRouter

from boba.agent.acp_server import AcpOperations, SessionHub
from boba.agent.acp_v1 import AcpV1
from boba.agent.acp_v2 import AcpV2
from boba.agent.acp_validator import AcpVersion


class AcpAgent:
    """Фабрики версий ACP для штатного AgentProtocolRouter SDK.

    Роутер SDK согласует версию и навсегда выбирает отдельный обработчик.
    Низкоуровневый Connection нужен для передачи готового JSON из SQL.
    """

    def __init__(
        self,
        hub: SessionHub,
        owner: str,
        granted: frozenset[str],
        version: str,
        *,
        maximum_protocol_version: AcpVersion = AcpVersion.V2,
    ) -> None:
        self._operations = AcpOperations(hub, owner, granted, version)
        self._protocol: AcpV1 | AcpV2 | None = None
        v2 = self._v2
        if maximum_protocol_version is AcpVersion.V1:
            v2 = None

        self._router = AgentProtocolRouter(v1=self._v1, v2=v2)

    def _v1(self, connection: V1Connection) -> AcpV1:
        protocol = AcpV1(self._operations)
        self._protocol = protocol

        return protocol

    def _v2(self, connection: V2Connection) -> AcpV2:
        protocol = AcpV2(self._operations)
        self._protocol = protocol

        return protocol

    def connect(
        self, input_stream, output_stream=None, **kwargs
    ) -> AgentProtocolConnection:
        connection = self._router.connect(input_stream, output_stream, **kwargs)
        transport = connection._connection
        self._operations.attach(transport)
        negotiate = transport._handler

        async def initialize(method, params, is_notification):
            result = await negotiate(method, params, is_notification)
            if isinstance(self._protocol, AcpV1):
                selected = getattr(negotiate, "_selected", None)
                if not isinstance(selected, MessageRouter):
                    raise RuntimeError("ACP SDK did not install the v1 message router")

                # В SDK rc3 negotiation не передаёт use_unstable_protocol в v1 attach.
                selected.route_request(
                    "mcp/message",
                    v1_schema.MessageMcpRequest,
                    self._protocol,
                    "mcp_message",
                )
                selected.route_notification(
                    "mcp/message",
                    v1_schema.MessageMcpNotification,
                    self._protocol,
                    "notify_mcp",
                )

            transport._handler = negotiate
            return result

        transport._handler = initialize

        return connection

    @property
    def protocol_version(self) -> AcpVersion:
        if self._protocol is None:
            raise RuntimeError("ACP protocol has not been negotiated")

        return self._protocol.version

    def busy(self) -> bool:
        return self._operations.busy()

    async def close_all(self) -> None:
        await self._operations.close_all()
