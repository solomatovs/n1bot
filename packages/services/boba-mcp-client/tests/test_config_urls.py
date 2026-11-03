"""Компактные URL в конфиге сохраняют адрес и границу префикса службы."""

import pytest
from pydantic import ValidationError

from boba.mcp_client.client import HttpLocation, HttpRoute


@pytest.mark.parametrize(
    "url",
    [
        "https://agent.example/boba/acp",
        "http://localhost:8660/acp",
        "http://[::1]:8660/boba/acp",
    ],
)
def test_route_url_round_trip(url: str) -> None:
    route = HttpRoute.model_validate(url)

    assert route.url() == url
    assert route.path == "/acp"


@pytest.mark.parametrize(
    "url",
    [
        "https://user:password@agent.example/boba",
        "https://agent.example/boba?token=value",
        "https://agent.example/boba#fragment",
    ],
)
def test_service_url_has_no_credentials_query_or_fragment(url: str) -> None:
    with pytest.raises(ValidationError):
        HttpLocation.model_validate(url)
