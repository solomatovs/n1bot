import pytest
from acp.experimental.v2 import schema
from acp.ws.client import create_websocket_stream
from agent_stand import ServiceHost
from websockets.exceptions import InvalidStatus

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


async def test_websocket_rejects_missing_token(service: ServiceHost) -> None:
    with pytest.raises(InvalidStatus) as failure:
        await create_websocket_stream(service.url)

    assert failure.value.response.status_code == 403


async def test_websocket_negotiates_v2_and_creates_session(
    service: ServiceHost,
) -> None:
    async with service.connection() as connection:
        initialized = await connection.send_request(
            "initialize",
            {"protocolVersion": 2, "info": {"name": "agent-test", "version": "1"}},
        )
        response = schema.InitializeResponse.model_validate(initialized)
        assert response.protocol_version == 2
        assert "systemPrompt" not in initialized["capabilities"]["session"]
        created = await connection.send_request("session/new", {"cwd": "/workspace"})
        assert "availableCommands" not in created
        session_response = schema.NewSessionResponse.model_validate(created)
        assert session_response.config_options
        owner = str(service.tokens.user_id)
        session = await service.agents.hub.store.session(
            session_response.session_id, owner
        )
        assert session["owner_id"] == owner
