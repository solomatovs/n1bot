"""Согласование версии проверяется на точке входа, без клиентов протокольных тестов."""

import pytest
from acp._transport import memory_transport_pair
from acp.connection import Connection
from agent_stand import AgentSystem

from boba.agent.acp_connection import AcpAgent
from boba.agent.acp_validator import AcpVersion

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


@pytest.mark.parametrize(
    "offered,maximum,selected", [(1, 1, 1), (2, 1, 1), (1, 2, 1), (2, 2, 2)]
)
async def test_connection_negotiates_one_protocol(
    system: AgentSystem, offered: int, maximum: int, selected: int
) -> None:
    adapter = AcpAgent(
        system.hub,
        system.owner,
        frozenset({"test"}),
        "test",
        maximum_protocol_version=AcpVersion(maximum),
    )
    agent_end, client_end = memory_transport_pair()
    server = adapter.connect(agent_end)

    async def receive(method, params, is_notification):
        raise AssertionError(f"Unexpected message during negotiation: {method}")

    connection = Connection(receive, client_end)
    try:
        response = await connection.send_request(
            "initialize",
            {
                "protocolVersion": offered,
                "info": {"name": "negotiation-test", "version": "1"},
            },
        )
        assert response["protocolVersion"] == selected
        assert adapter.protocol_version == selected
    finally:
        await adapter.close_all()
        await connection.close()
        await server.close()
