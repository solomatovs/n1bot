"""Подстановка профиля в параметр-соединение: whitelist субъекта на вызов.

Соединения приходят из таблицы, поэтому здесь они подменены хранилищем в
памяти: проверяется сама обвязка — что модель видит имя, тело получает
профиль, чужое имя отвергается, а два параметра резолвятся независимо.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Annotated, Any, Literal
from uuid import UUID, uuid4

import pytest
from langchain_core.tools import BaseTool, StructuredTool
from pydantic import SecretStr, create_model

from boba.cancellation import RunCancellation
from boba.connection_broker.user_connections import UserConnections
from boba.connections.base import ClientIdentity, ConnectionProfileBase
from boba.connections.manifest import ConnectionTypeManifest, ConnectionTypes
from boba.connections.marks import ConnectionRefusal
from boba.connections.profile import StoredConnection
from boba.identity.context import (
    CallContext,
    HumanInitiator,
    NoUserCredential,
    Scope,
    ScopeKind,
    Subject,
)
from boba.identity.errors import RefusalError
from boba.toolkit.entry import ToolArgv
from boba.toolkit.facade import UserConnection
from boba.toolrun.injected import ToolConfigError
from boba.toolrun.wrapping import ToolSchema

pytestmark = pytest.mark.anyio

SECRET = "probe-secret-value"


class ProbeConnection(ConnectionProfileBase):
    """Профиль выдуманного типа: чтобы тест не зависел от установленных пакетов."""

    kind: Literal["probe"] = "probe"
    host: str
    password: SecretStr
    client: str = ""

    def trace(self) -> str:
        return f"auth=password host={self.host}"

    def labeled(self, client: ClientIdentity) -> ProbeConnection:
        return self.model_copy(update={"client": client.login})


class OtherConnection(ConnectionProfileBase):
    """Второй тип: нужен, чтобы проверить выбор строк по виду соединения."""

    kind: Literal["other"] = "other"
    host: str

    def trace(self) -> str:
        return f"host={self.host}"


async def _probe(profile: ConnectionProfileBase) -> str:
    return "ok"


TYPES = ConnectionTypes(
    {
        "probe": ConnectionTypeManifest(
            kind="probe", profile=ProbeConnection, probe=_probe
        ),
        "other": ConnectionTypeManifest(
            kind="other", profile=OtherConnection, probe=_probe
        ),
    }
)


class Rows:
    """Хранилище в памяти на месте таблицы connections."""

    def __init__(self, rows: Sequence[StoredConnection]) -> None:
        self._rows = list(rows)

    async def for_subject(
        self, subject: Subject, kind: str
    ) -> Sequence[StoredConnection]:
        found: list[StoredConnection] = []
        for row in self._rows:
            if row.kind == kind:
                found.append(row)

        return found


class Credentials:
    """Источник кредов вызова: kerberos-секций у пробного типа нет."""

    async def for_connection(
        self, profile: ConnectionProfileBase, credential: object
    ) -> ConnectionProfileBase:
        return profile


def _row(name: str, profile: ConnectionProfileBase, row_id: UUID | None = None):
    return StoredConnection(id=row_id or uuid4(), name=name, profile=profile)


def _probe_row(name: str, host: str) -> StoredConnection:
    return _row(name, ProbeConnection(host=host, password=SecretStr(SECRET)))


def _tool(name: str, fields: dict[str, Any]) -> BaseTool:
    """Инструмент, чьё тело возвращает полученные аргументы как есть."""
    schema = create_model(f"{name}_args", **fields)

    async def body(**kwargs: object) -> dict[str, object]:
        return kwargs

    return StructuredTool(
        name=name, description=name, args_schema=schema, coroutine=body
    )


def _one_connection() -> BaseTool:
    return _tool(
        "probe_query",
        {
            "connection": (Annotated[ProbeConnection, UserConnection], ...),
            "sql": (str, ...),
        },
    )


def _two_connections() -> BaseTool:
    return _tool(
        "probe_copy",
        {
            "source": (Annotated[ProbeConnection, UserConnection], ...),
            "target": (Annotated[ProbeConnection, UserConnection], ...),
        },
    )


def _bound(tool: BaseTool, rows: Sequence[StoredConnection]) -> BaseTool:
    store = Rows(rows)
    UserConnections.bind_all(
        [tool],
        lambda: store,  # type: ignore[arg-type]
        Credentials,  # type: ignore[arg-type]
        lambda: TYPES,
    )

    return tool


def _subject() -> Subject:
    return Subject(
        user_id=uuid4(), login="ivanov", roles=frozenset({"read"}), profile="default"
    )


def _context() -> CallContext:
    return CallContext(
        subject=_subject(),
        scope=Scope(kind=ScopeKind.CHAT, id="t1"),
        initiator=HumanInitiator(via="api"),
        credential=NoUserCredential(reason="test"),
        cancellation=RunCancellation(),
    )


async def _call(tool: BaseTool, args: dict[str, Any]) -> dict[str, Any]:
    token = CallContext.push(_context())
    try:
        return await tool.ainvoke(args)
    finally:
        CallContext.pop(token)


class TestSchemaShownToTheModel:
    def test_profile_parameter_becomes_a_name(self) -> None:
        tool = _bound(_one_connection(), [_probe_row("main", "db.local")])

        schema = ToolSchema.of(tool)
        assert schema is not None
        assert schema.model_fields["connection"].annotation is str

    def test_no_connection_fields_are_left_in_the_shown_schema(self) -> None:
        tool = _bound(_one_connection(), [_probe_row("main", "db.local")])

        schema = ToolSchema.of(tool)
        assert schema is not None
        assert not ToolArgv.connection_fields(schema)


class TestProfileReachesTheBody:
    async def test_named_row_is_substituted(self) -> None:
        tool = _bound(_one_connection(), [_probe_row("main", "db.local")])

        got = await _call(tool, {"connection": "main", "sql": "select 1"})

        profile = got["connection"]
        assert isinstance(profile, ProbeConnection)
        assert profile.host == "db.local"
        assert profile.password.get_secret_value() == SECRET

    async def test_profile_is_signed_by_the_caller(self) -> None:
        tool = _bound(_one_connection(), [_probe_row("main", "db.local")])

        got = await _call(tool, {"connection": "main", "sql": "select 1"})

        assert got["connection"].client == "ivanov"

    async def test_two_parameters_resolve_independently(self) -> None:
        rows = [_probe_row("left", "a.local"), _probe_row("right", "b.local")]
        tool = _bound(_two_connections(), rows)

        got = await _call(tool, {"source": "left", "target": "right"})

        assert got["source"].host == "a.local"
        assert got["target"].host == "b.local"


class TestRefusals:
    async def test_unknown_name_is_refused_with_the_available_ones(self) -> None:
        tool = _bound(_one_connection(), [_probe_row("main", "db.local")])

        with pytest.raises(RefusalError) as caught:
            await _call(tool, {"connection": "нет-такого", "sql": "select 1"})

        assert caught.value.kind == ConnectionRefusal.NOT_VISIBLE
        assert "main" in str(caught.value)

    async def test_duplicate_name_is_refused(self) -> None:
        rows = [_probe_row("dup", "a.local"), _probe_row("dup", "b.local")]
        tool = _bound(_one_connection(), rows)

        with pytest.raises(RefusalError) as caught:
            await _call(tool, {"connection": "dup", "sql": "select 1"})

        assert caught.value.kind == ConnectionRefusal.AMBIGUOUS

    async def test_row_of_another_kind_is_invisible(self) -> None:
        tool = _bound(_one_connection(), [_row("web", OtherConnection(host="h"))])

        with pytest.raises(RefusalError) as caught:
            await _call(tool, {"connection": "web", "sql": "select 1"})

        assert caught.value.kind == ConnectionRefusal.NOT_VISIBLE


class TestDeclarationIsChecked:
    def test_parameter_must_be_a_profile_model(self) -> None:
        tool = _tool("broken", {"connection": (Annotated[str, UserConnection], ...)})

        with pytest.raises(ToolConfigError, match="not a connection profile"):
            _bound(tool, [])

    def test_type_package_must_be_installed(self) -> None:
        class Unregistered(ConnectionProfileBase):
            kind: Literal["unregistered"] = "unregistered"

            def trace(self) -> str:
                return "unregistered"

        tool = _tool(
            "broken", {"connection": (Annotated[Unregistered, UserConnection], ...)}
        )

        with pytest.raises(ToolConfigError, match="not installed"):
            _bound(tool, [])
