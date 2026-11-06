"""Сборка endpoint'а: объявления соответствуют маршрутам и провайдерам по HTTP."""

from __future__ import annotations

import socket
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from service_stand import PROFILE, ServiceStand

from boba.mcp_server.catalog import EndpointCatalog
from boba.mcp_server.schemas import EndpointSchemas
from boba.mcp_server.server import FileUploadSchema, FileUploadTool, RunLimits
from boba.toolkit.wire import FilesFeature, JournalFeature

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


@pytest.mark.parametrize("mode", ["auto", "legacy"])
@pytest.mark.parametrize("journaled", [False, True])
@pytest.mark.parametrize("prompted", [False, True])
@pytest.mark.parametrize("upload", [False, True])
async def test_endpoint_announcements_match_installed_components(
    tmp_path: Path, mode: str, journaled: bool, prompted: bool, upload: bool
) -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    limits = RunLimits(max_runs=4, max_waiting=4)
    stand = ServiceStand(tmp_path, limits, port, "", journaled=journaled)
    endpoint = stand.catalog.endpoints()[0]
    selected = ["fake_echo"]
    if upload:
        selected.append(FileUploadTool.NAME)

    prompts = {}
    if prompted:
        prompts = endpoint.prompts

    endpoint = endpoint.model_copy(
        update={"tools": tuple(selected), "prompts": prompts}
    )
    snapshot = EndpointCatalog((endpoint,))
    stand.catalog.replace(snapshot)
    stand.endpoints = stand.build(snapshot)

    async with stand.serving():
        token = await stand.dev_token(PROFILE)
        transport = StreamableHttpTransport(stand.url(PROFILE), auth=token)
        async with Client(transport, mode=mode) as client:
            capabilities = client.server_capabilities
            assert capabilities is not None
            assert (capabilities.resources is not None) is journaled
            assert (capabilities.prompts is not None) is prompted
            if capabilities.resources is not None:
                assert capabilities.resources.subscribe is (mode == "auto")
                assert capabilities.resources.list_changed is (mode == "auto")

            listed_prompts = await client.list_prompts()
            assert bool(listed_prompts) is prompted
            if prompted:
                rendered = await client.get_prompt(
                    ServiceStand.PROMPT, {"sql": "select 1"}
                )
                assert rendered.messages

            tools = {tool.name: tool for tool in await client.list_tools()}
            assert set(tools) == set(selected)
            if mode == "auto":
                extensions = capabilities.extensions
                assert extensions is not None
                assert (FilesFeature.ID in extensions) is upload
                assert (JournalFeature.ID in extensions) is journaled

        # HTTP-маршрут файлов существует даже без операции file_upload.
        scope = str(uuid4())
        address = f"{stand.url(PROFILE)}/files/{scope}/upload/probe.txt"
        async with httpx.AsyncClient() as http:
            reply = await http.get(address)
            assert reply.status_code == 401

            headers = {"Authorization": f"Bearer {token}"}
            stored = await http.put(address, content=b"assembly", headers=headers)
            assert stored.status_code == 201
            downloaded = await http.get(address, headers=headers)
            assert downloaded.status_code == 200
            assert downloaded.content == b"assembly"


@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_file_upload_export_matches_published_tool(
    tmp_path: Path, mode: str
) -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    limits = RunLimits(max_runs=4, max_waiting=4)
    stand = ServiceStand(tmp_path, limits, port, "")
    schemas = EndpointSchemas()
    definition = FileUploadSchema()
    exported = schemas.export({definition.tool.name: definition.tool})

    async with stand.serving():
        token = await stand.dev_token(PROFILE)
        transport = StreamableHttpTransport(stand.url(PROFILE), auth=token)
        async with Client(transport, mode=mode) as client:
            tools = {tool.name: tool for tool in await client.list_tools()}

    actual = tools[FileUploadTool.NAME].model_dump(
        mode="json", by_alias=True, exclude_none=True
    )
    assert exported[FileUploadTool.NAME]["mcp"] == actual
