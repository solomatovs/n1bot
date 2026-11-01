"""Разбор формы MCP-сервера и сборка записи с сохранёнными секретами.

Ошибки:
McpAppError — форма нарушает контракт; значения секретов в ответ не входят.
"""

from collections.abc import Mapping, Sequence
from enum import IntEnum
from typing import Annotated, Literal, TypeVar
from urllib.parse import SplitResult, urlsplit

import httpx2
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    StringConstraints,
    ValidationError,
)

from boba.aid.mcp_servers import McpAppError, McpAuthChoice, ReservedHeader
from boba.aid.mcp_servers.config import McpUserConfig
from boba.aid.mcp_servers.store import StoredServer
from boba.core.mcp_names import McpToolNames
from boba.mcp_client.client import (
    BearerAuth,
    HttpEndpoint,
    HttpLocation,
    HttpRoute,
    McpServerLimits,
    NoAuth,
    SseEndpoint,
)

ServerName = Annotated[str, StringConstraints(pattern=r"^[a-zA-Z0-9_-]{1,64}$")]
FormModel = TypeVar("FormModel", bound=BaseModel)


class HeaderField(BaseModel):
    """Имя и секрет заголовка из браузера; пустой секрет сохраняет прежний."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(pattern=r"^[A-Za-z0-9-]{1,64}$")
    value: SecretStr


class McpServerForm(BaseModel):
    """Проводная форма пользовательского сервера без сроков исполнения."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: ServerName
    transport: Literal["streamable-http", "sse"]
    url: str
    auth: McpAuthChoice
    token: SecretStr
    headers: Sequence[HeaderField]


class McpServerAdd(BaseModel):
    """Запрос добавления сервера из панели."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    form: McpServerForm


class McpServerUpdate(McpServerAdd):
    """Запрос замены записи, адресуемой именем до правки."""

    current: ServerName


class McpServerToggle(BaseModel):
    """Запрос включения или выключения своего сервера."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: ServerName
    enabled: bool = Field(strict=True)


class McpServerFormView(BaseModel):
    """Данные формы правки: только адрес, вид входа и имена заголовков."""

    model_config = ConfigDict(frozen=True)

    name: str
    transport: Literal["streamable-http", "sse"]
    url: str
    auth: Literal["none", "bearer", "local", "proxy"]
    editable: bool
    has_token: bool
    headers: Sequence[str]


class HeaderCode(IntEnum):
    """Границы печатных ASCII-символов значения HTTP-заголовка."""

    SPACE = 32
    DELETE = 127


class McpForms:
    """Граница браузера: проверяет форму и собирает StoredServer для каталога.

    Каталог создаёт её с настройками сроков; сохранённые секреты остаются
    внутри процесса и используются лишь для пустых полей формы правки.
    """

    def __init__(self, config: McpUserConfig) -> None:
        self._limits = McpServerLimits(
            connect_timeout_sec=config.connect_timeout_sec,
            call_timeout_sec=config.call_timeout_sec,
        )
        self._names = McpToolNames()
        reserved = map(str.lower, ReservedHeader)
        self._reserved = frozenset(reserved)

    def parse(self, model: type[FormModel], raw: Mapping[str, object]) -> FormModel:
        try:
            return model.model_validate(raw)
        except ValidationError as exc:
            fields = self.fields(exc)
            raise McpAppError(f"MCP request: invalid fields: {fields}") from None

    @staticmethod
    def fields(exc: ValidationError) -> str:
        paths = []
        for error in exc.errors(include_input=False, include_context=False):
            parts = map(str, error["loc"])
            path = ".".join(parts)
            if not path:
                path = "<root>"

            paths.append(path)

        return ", ".join(paths)

    def server(
        self, form: McpServerForm, stored: StoredServer | None = None
    ) -> StoredServer:
        self._check_name(form.name)
        route = self._address(form.url)
        auth = self._auth(form, stored)
        headers = self._headers(form.headers, stored)
        limits = self._limits
        if stored is not None:
            limits = stored.limits

        try:
            endpoint: HttpEndpoint | SseEndpoint
            if form.transport == "sse":
                endpoint = SseEndpoint(
                    service=route.service, path=route.path, auth=auth, headers=headers
                )
            else:
                endpoint = HttpEndpoint(
                    service=route.service, path=route.path, auth=auth, headers=headers
                )

            return StoredServer(endpoint=endpoint, limits=limits)
        except ValidationError as exc:
            fields = self.fields(exc)
            raise McpAppError(
                f"Saving MCP server: invalid credentials in fields {fields}"
            ) from None

    def _check_name(self, name: str) -> None:
        if name == "chat":
            raise McpAppError(
                "Saving MCP server 'chat': this name belongs to "
                "the built-in chat server"
            )

        try:
            self._names.checked_server(name)
        except ValueError as exc:
            raise McpAppError(f"Saving MCP server {name!r}: {exc}") from exc

    def _address(self, url: str) -> HttpRoute:
        try:
            location = urlsplit(url)
            host = location.hostname
            port = location.port
        except ValueError:
            raise McpAppError(
                "Saving MCP server: URL expects a valid host and port"
            ) from None

        scheme = location.scheme
        if scheme not in ("http", "https"):
            raise McpAppError("Saving MCP server: URL expects http or https")

        if not host:
            raise McpAppError("Saving MCP server: URL expects a non-empty host")

        self._clean_address(url, location)
        if port is None:
            port = 80
            if scheme == "https":
                port = 443

        path = location.path
        if not path:
            path = "/"

        try:
            service = HttpLocation(scheme=scheme, host=host, port=port, path="")

            route = HttpRoute(service=service, path=path)
            route.url()

            return route
        except httpx2.InvalidURL:
            raise McpAppError(
                "Saving MCP server: URL contains an invalid host or path"
            ) from None
        except ValidationError as exc:
            fields = self.fields(exc)
            raise McpAppError(
                f"Saving MCP server: invalid URL in fields {fields}"
            ) from None

    @staticmethod
    def _clean_address(url: str, location: SplitResult) -> None:
        if location.username is not None:
            raise McpAppError("Saving MCP server: URL must not contain credentials")

        if location.password is not None:
            raise McpAppError("Saving MCP server: URL must not contain credentials")

        if location.query:
            raise McpAppError("Saving MCP server: URL must not contain a query")

        if location.fragment:
            raise McpAppError("Saving MCP server: URL must not contain a fragment")

        for char in url:
            code = ord(char)
            if code < HeaderCode.SPACE:
                raise McpAppError(
                    "Saving MCP server: URL must not contain control characters"
                )

            if code == HeaderCode.DELETE:
                raise McpAppError(
                    "Saving MCP server: URL must not contain control characters"
                )

            if char.isspace():
                raise McpAppError("Saving MCP server: URL must not contain whitespace")

            if char == "\\":
                raise McpAppError("Saving MCP server: URL must not contain backslashes")

    def view(
        self, name: str, stored: StoredServer, editable: bool
    ) -> McpServerFormView:
        endpoint = stored.endpoint
        has_token = False
        if isinstance(endpoint.auth, BearerAuth):
            has_token = bool(endpoint.auth.token.get_secret_value())

        return McpServerFormView(
            name=name,
            transport=endpoint.transport,
            url=endpoint.url(),
            auth=endpoint.auth.auth,
            editable=editable,
            has_token=has_token,
            headers=tuple(endpoint.headers),
        )

    def _auth(
        self, form: McpServerForm, stored: StoredServer | None
    ) -> NoAuth | BearerAuth:
        if form.auth is McpAuthChoice.NONE:
            return NoAuth()

        token = form.token
        if not token.get_secret_value():
            if stored is not None:
                if isinstance(stored.endpoint.auth, BearerAuth):
                    token = stored.endpoint.auth.token

        if not token.get_secret_value():
            raise McpAppError("Saving MCP server: bearer token is required")

        self._header_value(token)

        return BearerAuth(token=token)

    def _headers(
        self, fields: Sequence[HeaderField], stored: StoredServer | None
    ) -> Mapping[str, SecretStr]:
        previous: dict[str, SecretStr] = {}
        if stored is not None:
            for name, value in stored.endpoint.headers.items():
                previous[name.lower()] = value

        headers: dict[str, SecretStr] = {}
        seen: set[str] = set()
        for field in fields:
            name = field.name.lower()
            if name in self._reserved:
                raise McpAppError(
                    f"Saving MCP server: header {field.name!r} is reserved "
                    "by the transport"
                )

            if name in seen:
                raise McpAppError(f"Saving MCP server: duplicate header {field.name!r}")

            seen.add(name)
            value = field.value
            if not value.get_secret_value():
                saved = previous.get(name)
                if saved is None:
                    raise McpAppError(
                        f"Saving MCP server: value of header {field.name!r} is empty"
                    )

                value = saved

            self._header_value(value)
            headers[field.name] = value

        return headers

    @staticmethod
    def _header_value(value: SecretStr) -> None:
        raw = value.get_secret_value()
        if raw != raw.strip():
            raise McpAppError(
                "Saving MCP server: credentials must not start or end with whitespace"
            )

        if not raw.isascii():
            raise McpAppError(
                "Saving MCP server: credentials must contain "
                "HTTP header ASCII characters"
            )

        for char in raw:
            code = ord(char)
            if code < HeaderCode.SPACE:
                raise McpAppError(
                    "Saving MCP server: credentials must not contain control characters"
                )

            if code == HeaderCode.DELETE:
                raise McpAppError(
                    "Saving MCP server: credentials must not contain control characters"
                )
