"""Возврат OAuth сохраняет адрес страницы и остаётся внутри приложения AID."""

import pytest
from pydantic import AnyHttpUrl, ValidationError

from boba.aid.mcp_servers import McpAppError
from boba.aid.mcp_servers.form import McpOAuthStart
from boba.aid.mcp_servers.oauth import OAuthReturnUrls


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    """Проверка адресов не использует сессию Chainlit."""


@pytest.mark.parametrize(
    "path",
    [
        "/ss/aid",
        "/ss/aid/",
        "/ss/aid/thread/first",
        "/ss/aid/thread/second?view=tools&filter=a%2Fb#tool-card",
    ],
)
def test_return_preserves_the_source_page(path: str) -> None:
    origin = "https://example.test"
    returns = OAuthReturnUrls(AnyHttpUrl(origin + "/ss/aid/"))
    request = McpOAuthStart(name="warehouse", return_url=AnyHttpUrl(origin + path))
    assert returns.checked(request.return_url) == origin + path


@pytest.mark.parametrize(
    "url",
    [
        "https://other.test/ss/aid/thread/first",
        "http://example.test/ss/aid/thread/first",
        "https://example.test:8443/ss/aid/thread/first",
        "https://example.test/ss/aid-other/thread/first",
        "https://example.test/ss/other",
        "https://example.test/ss/aid/../other",
        "https://example.test/ss/aid/%2e%2e/other",
        "https://example.test/ss/aid/a%2F..%2F..%2Fother",
        "https://person@example.test/ss/aid/",
        "https://example.test@other.test/ss/aid/",
    ],
)
def test_return_rejects_another_origin_or_application(url: str) -> None:
    returns = OAuthReturnUrls(AnyHttpUrl("https://example.test/ss/aid/"))
    with pytest.raises(McpAppError, match="OAuth return URL"):
        returns.checked(AnyHttpUrl(url))


def test_return_without_an_application_prefix() -> None:
    returns = OAuthReturnUrls(AnyHttpUrl("http://127.0.0.1:8501/"))
    target = "http://127.0.0.1:8501/thread/first?view=mcp#tools"
    assert returns.checked(AnyHttpUrl(target)) == target


def test_start_requires_the_page_instead_of_defaulting_to_home() -> None:
    with pytest.raises(ValidationError, match="return_url"):
        McpOAuthStart.model_validate({"name": "warehouse"})
