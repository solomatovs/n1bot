"""Подстановка профиля в параметр-соединение: whitelist субъекта на вызов.

Соединения приходят из таблицы, поэтому здесь они подменены хранилищем в
памяти: проверяется сама обвязка — что модель видит имя, тело получает
профиль, чужое имя отвергается, а два параметра резолвятся независимо.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from typing import Annotated, Literal
from uuid import UUID, uuid4

import pytest
from langchain_core.tools import BaseTool
from probe_stand import SECRET, TYPES, OtherConnection, ProbeConnection, ProbeTools
from pydantic import SecretStr

from boba.connection_broker.user_connections import UserConnections
from boba.connections.base import ConnectionBase
from boba.connections.marks import ConnectionRefusal
from boba.connections.stored import GrantedConnection, StoredConnection
from boba.identity.context import (
    Subject,
)
from boba.identity.errors import RefusalError
from boba.toolkit.entry import ToolArgv
from boba.toolkit.facade import UserConnection
from boba.toolrun.injected import ToolConfigError
from boba.toolrun.wrapping import ToolSchema

pytestmark = pytest.mark.anyio

TOOLS = ProbeTools()


class Rows:
    """Хранилище в памяти на месте таблицы connections."""

    def __init__(self, rows: Sequence[StoredConnection]) -> None:
        self._rows = list(rows)

    async def for_subject(
        self, subject: Subject, kind: str
    ) -> Sequence[GrantedConnection]:
        """Как SubjectGrantsQuery: дубль — одно имя дважды внутри вида."""
        names: Counter[str] = Counter()
        for row in self._rows:
            if row.kind == kind:
                names[row.name] += 1

        found: list[GrantedConnection] = []
        for row in self._rows:
            if row.kind != kind:
                continue

            found.append(GrantedConnection(row=row, ambiguous=names[row.name] > 1))

        return found


class Credentials:
    """Источник кредов вызова: kerberos-секций у пробного типа нет."""

    async def for_connection(
        self, connection: ConnectionBase, credential: object
    ) -> ConnectionBase:
        return connection


def _row(name: str, connection: ConnectionBase, row_id: UUID | None = None):
    return StoredConnection(id=row_id or uuid4(), name=name, connection=connection)


def _probe_row(name: str, host: str) -> StoredConnection:
    return _row(name, ProbeConnection(host=host, password=SecretStr(SECRET)))


def _bound(tool: BaseTool, rows: Sequence[StoredConnection]) -> BaseTool:
    store = Rows(rows)
    UserConnections.bind_all(
        [tool],
        lambda: store,  # type: ignore[arg-type]
        Credentials,  # type: ignore[arg-type]
        lambda: TYPES,
    )

    return tool


class TestSchemaShownToTheModel:
    def test_profile_parameter_becomes_a_name(self) -> None:
        tool = _bound(TOOLS.one_connection(), [_probe_row("main", "db.local")])

        schema = ToolSchema.of(tool)
        assert schema is not None
        assert schema.model_fields["connection"].annotation is str

    def test_no_connection_fields_are_left_in_the_shown_schema(self) -> None:
        tool = _bound(TOOLS.one_connection(), [_probe_row("main", "db.local")])

        schema = ToolSchema.of(tool)
        assert schema is not None
        assert not ToolArgv.connection_fields(schema)


class TestProfileReachesTheBody:
    async def test_named_row_is_substituted(self) -> None:
        tool = _bound(TOOLS.one_connection(), [_probe_row("main", "db.local")])

        got = await TOOLS.call(tool, {"connection": "main", "sql": "select 1"})

        connection = got["connection"]
        assert isinstance(connection, ProbeConnection)
        assert connection.host == "db.local"
        assert connection.password.get_secret_value() == SECRET

    async def test_profile_is_signed_by_the_caller(self) -> None:
        tool = _bound(TOOLS.one_connection(), [_probe_row("main", "db.local")])

        got = await TOOLS.call(tool, {"connection": "main", "sql": "select 1"})

        assert got["connection"].client == "ivanov"

    async def test_two_parameters_resolve_independently(self) -> None:
        rows = [_probe_row("left", "a.local"), _probe_row("right", "b.local")]
        tool = _bound(TOOLS.two_connections(), rows)

        got = await TOOLS.call(tool, {"source": "left", "target": "right"})

        assert got["source"].host == "a.local"
        assert got["target"].host == "b.local"


class TestRefusals:
    async def test_unknown_name_is_refused_with_the_available_ones(self) -> None:
        tool = _bound(TOOLS.one_connection(), [_probe_row("main", "db.local")])

        with pytest.raises(RefusalError) as caught:
            await TOOLS.call(tool, {"connection": "нет-такого", "sql": "select 1"})

        assert caught.value.kind == ConnectionRefusal.NOT_VISIBLE
        assert "main" in str(caught.value)

    async def test_duplicate_name_is_refused(self) -> None:
        rows = [_probe_row("dup", "a.local"), _probe_row("dup", "b.local")]
        tool = _bound(TOOLS.one_connection(), rows)

        with pytest.raises(RefusalError) as caught:
            await TOOLS.call(tool, {"connection": "dup", "sql": "select 1"})

        assert caught.value.kind == ConnectionRefusal.AMBIGUOUS

    async def test_row_of_another_kind_is_invisible(self) -> None:
        tool = _bound(TOOLS.one_connection(), [_row("web", OtherConnection(host="h"))])

        with pytest.raises(RefusalError) as caught:
            await TOOLS.call(tool, {"connection": "web", "sql": "select 1"})

        assert caught.value.kind == ConnectionRefusal.NOT_VISIBLE


class TestDeclarationIsChecked:
    def test_parameter_must_be_a_connection_model(self) -> None:
        tool = TOOLS.tool(
            "broken", {"connection": (Annotated[str, UserConnection], ...)}
        )

        with pytest.raises(ToolConfigError, match="not a connection model"):
            _bound(tool, [])

    def test_type_package_must_be_installed(self) -> None:
        class Unregistered(ConnectionBase):
            kind: Literal["unregistered"] = "unregistered"

            def trace(self) -> str:
                return "unregistered"

        tool = TOOLS.tool(
            "broken", {"connection": (Annotated[Unregistered, UserConnection], ...)}
        )

        with pytest.raises(ToolConfigError, match="not installed"):
            _bound(tool, [])
