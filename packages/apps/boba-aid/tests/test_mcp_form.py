"""Разбор пользовательских MCP-форм без сетевых действий."""

import pytest
from pydantic import SecretStr

from boba.aid.mcp_servers import McpAppError, McpAuthChoice
from boba.aid.mcp_servers.config import McpUserConfig
from boba.aid.mcp_servers.form import HeaderField, McpForms, McpServerAdd, McpServerForm
from boba.mcp_client.client import BearerAuth, BrowserOAuthAuth


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    """Разбор формы не использует сессию chainlit."""


class TestMcpForms:
    def _form(self, url: str = "https://mcp.invalid") -> McpServerForm:
        return McpServerForm(
            name="valid-name",
            transport="streamable-http",
            url=url,
            auth=McpAuthChoice.NONE,
            token=SecretStr(""),
            headers=(),
        )

    @pytest.mark.parametrize(
        "url",
        [
            "ftp://mcp.invalid",
            "https://alice:secret@mcp.invalid",
            "https://mcp.invalid?key=secret",
            "https://mcp.invalid/#secret",
            "https:///mcp",
            "http://mcp.invalid:99999",
            "http://[broken",
            "https://mcp.invalid/line\nbreak",
            "https://mcp.invalid/a\\b",
        ],
    )
    def test_unsafe_address_is_rejected_without_echoing_values(self, url: str) -> None:
        with pytest.raises(McpAppError) as refused:
            McpForms(McpUserConfig()).server(self._form(url))

        assert "secret" not in str(refused.value)

    @pytest.mark.parametrize("name", ["chat", "a__b", "a_"])
    def test_reserved_or_ambiguous_name_is_rejected(self, name: str) -> None:
        with pytest.raises(McpAppError):
            McpForms(McpUserConfig()).server(
                self._form().model_copy(update={"name": name})
            )

    @pytest.mark.parametrize(
        ("url", "port"),
        [
            ("https://mcp.invalid", 443),
            ("http://mcp.invalid", 80),
            ("http://[::1]:8500/mcp", 8500),
        ],
    )
    def test_defaults_and_ipv6_are_preserved(self, url: str, port: int) -> None:
        server = McpForms(McpUserConfig()).server(self._form(url))
        assert server.endpoint.service.port == port
        assert server.endpoint.path.startswith("/")
        assert server.limits.connect_timeout_sec == 10
        assert server.limits.call_timeout_sec == 60

    def test_bearer_and_header_secrets_are_kept_on_edit(self) -> None:
        forms = McpForms(McpUserConfig(connect_timeout_sec=7, call_timeout_sec=91))
        form = self._form().model_copy(
            update={
                "auth": McpAuthChoice.BEARER,
                "token": SecretStr("secret"),
                "headers": (
                    HeaderField(name="X-Key", value=SecretStr("header-secret")),
                ),
            }
        )
        stored = forms.server(form)
        empty = form.model_copy(
            update={
                "token": SecretStr(""),
                "headers": (HeaderField(name="x-key", value=SecretStr("")),),
            }
        )
        changed = McpForms(McpUserConfig()).server(empty, stored)
        assert changed.limits == stored.limits
        assert changed.limits.call_timeout_sec == 91
        assert isinstance(changed.endpoint.auth, BearerAuth)
        assert changed.endpoint.auth.token.get_secret_value() == "secret"
        assert changed.endpoint.headers["x-key"].get_secret_value() == "header-secret"
        with pytest.raises(McpAppError, match="bearer token is required"):
            forms.server(empty)

    def test_browser_oauth_is_personal_and_needs_no_entered_token(self) -> None:
        forms = McpForms(McpUserConfig())
        form = self._form().model_copy(update={"auth": McpAuthChoice.OAUTH})
        server = forms.server(form)

        assert isinstance(server.endpoint.auth, BrowserOAuthAuth)
        assert server.endpoint.personal()
        view = forms.view(form.name, server, editable=True)
        assert view.auth == "oauth"
        assert not view.has_token

    def test_browser_oauth_requires_tls_outside_loopback(self) -> None:
        form = self._form("http://remote.example/mcp").model_copy(
            update={"auth": McpAuthChoice.OAUTH}
        )
        with pytest.raises(McpAppError, match="HTTPS outside loopback"):
            McpForms(McpUserConfig()).server(form)

    @pytest.mark.parametrize(
        "headers",
        [
            (HeaderField(name="authorization", value=SecretStr("secret")),),
            (HeaderField(name="Accept", value=SecretStr("secret")),),
            (
                HeaderField(name="X-Key", value=SecretStr("a")),
                HeaderField(name="x-key", value=SecretStr("b")),
            ),
            (HeaderField(name="X-Key", value=SecretStr("")),),
            (HeaderField(name="X-Key", value=SecretStr(" secret ")),),
            (HeaderField(name="X-Key", value=SecretStr("secret\r\nX-Injected: yes")),),
        ],
    )
    def test_invalid_headers_are_rejected(
        self, headers: tuple[HeaderField, ...]
    ) -> None:
        form = self._form().model_copy(update={"headers": headers})
        with pytest.raises(McpAppError) as refused:
            McpForms(McpUserConfig()).server(form)

        assert "secret" not in str(refused.value)

    def test_payload_validation_reports_fields_without_secret_values(self) -> None:
        raw = {"form": {"name": "valid", "token": {"secret": "never-echo-me"}}}
        with pytest.raises(McpAppError) as refused:
            McpForms(McpUserConfig()).parse(McpServerAdd, raw)

        assert "form.token" in str(refused.value)
        assert "never-echo-me" not in str(refused.value)
