import pytest
from acp import schema
from acp.ws.client import create_websocket_stream
from agent_stand import ServiceHost
from websockets.exceptions import InvalidStatus

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


async def test_websocket_rejects_missing_token(service: ServiceHost) -> None:
    with pytest.raises(InvalidStatus) as failure:
        await create_websocket_stream(service.url)

    assert failure.value.response.status_code == 403


async def test_websocket_negotiates_v1_and_creates_session(
    service: ServiceHost,
) -> None:
    async with service.connection() as connection:
        request = schema.InitializeRequest(
            protocol_version=1, client_capabilities=schema.ClientCapabilities()
        )
        initialized = await connection.send_request(
            "initialize",
            request.model_dump(mode="json", by_alias=True, exclude_none=True),
        )
        response = schema.InitializeResponse.model_validate(initialized)
        assert response.protocol_version == 1
        assert response.agent_capabilities is not None
        assert response.agent_capabilities.load_session
        result = await connection.send_request(
            "session/new", {"cwd": "/workspace", "mcpServers": []}
        )
        created = schema.NewSessionResponse.model_validate(result)
        assert created.config_options
