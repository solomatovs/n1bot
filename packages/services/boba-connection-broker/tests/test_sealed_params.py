"""Приём запечатанных соединений исполнителем: модель видит ссылку, тело
получает профиль из значения, запечатанного ключом исполнителя; незапечатанное,
чужое, просроченное и соединение другого вида отвергаются с подсказкой."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from langchain_core.tools import BaseTool
from probe_stand import (
    LOGIN,
    SECRET,
    TYPES,
    OtherConnection,
    ProbeConnection,
    ProbeTools,
)
from pydantic import SecretStr

from boba.connection_broker.sealed import SealedConnectionParams
from boba.connections.base import ConnectionBase
from boba.connections.marks import ConnectionRefusal
from boba.connections.sealed import (
    ConnectionSeal,
    SealedConnection,
    SealFeature,
    SealKey,
    SealKeys,
)
from boba.identity.errors import RefusalError
from boba.toolkit.entry import ToolArgv
from boba.toolkit.types import SecretReveal
from boba.toolrun.wrapping import ToolSchema

pytestmark = pytest.mark.anyio

TOOLS = ProbeTools()


def _bound(tool: BaseTool, keys: SealKeys) -> BaseTool:
    SealedConnectionParams(keys, lambda: TYPES).bind_all([tool])

    return tool


def _probe(host: str) -> ConnectionBase:
    return ProbeConnection(host=host, password=SecretStr(SECRET))


def _sealed(
    key: SealKey,
    profile: ConnectionBase,
    *,
    login: str = LOGIN,
    ttl: timedelta = timedelta(hours=1),
) -> str:
    """Соединение, запечатанное клиентом ключом исполнителя."""
    sealed = SealedConnection(
        login=login,
        expires_at=datetime.now(UTC) + ttl,
        profile=SecretReveal.dumped(profile),
    )

    return ConnectionSeal(key).seal(sealed)


async def _refusal(keys: SealKeys, connection: str) -> RefusalError:
    tool = _bound(TOOLS.one_connection(), keys)

    with pytest.raises(RefusalError) as refused:
        await TOOLS.call(tool, {"connection": connection, "sql": "x"})

    return refused.value


class TestSchemaShownToTheModel:
    def test_profile_parameter_becomes_a_reference(self) -> None:
        tool = _bound(TOOLS.one_connection(), SealKeys())

        schema = ToolSchema.of(tool)
        if schema is None:
            raise AssertionError("схема инструмента пропала")

        field = schema.model_fields["connection"]
        if field.annotation is not str:
            raise AssertionError(f"модель видит строку: {field.annotation}")
        if ToolArgv.connection_fields(schema):
            raise AssertionError("маркер соединения со схемы снят")

        description = str(field.description)
        if "connection_list" not in description:
            raise AssertionError(f"описание ведёт к connection_list: {description}")


class TestSealedValueReachesTheBody:
    async def test_body_gets_the_profile_with_its_secret(self) -> None:
        keys = SealKeys()
        tool = _bound(TOOLS.one_connection(), keys)
        sealed = _sealed(keys.public(), _probe("db.local"))

        got = await TOOLS.call(tool, {"connection": sealed, "sql": "x"})

        connection = got["connection"]
        if not isinstance(connection, ProbeConnection):
            raise AssertionError(f"тело получило профиль: {connection!r}")
        if connection.host != "db.local":
            raise AssertionError(f"профиль из запечатанного значения: {connection}")
        if connection.password.get_secret_value() != SECRET:
            raise AssertionError("секрет профиля дошёл до тела")
        if connection.client != LOGIN:
            raise AssertionError(f"профиль подписан вызывающим: {connection.client!r}")

    async def test_two_parameters_are_opened_independently(self) -> None:
        keys = SealKeys()
        tool = _bound(TOOLS.two_connections(), keys)
        args = {
            "source": _sealed(keys.public(), _probe("src.local")),
            "target": _sealed(keys.public(), _probe("dst.local")),
        }

        got = await TOOLS.call(tool, args)

        hosts = (got["source"].host, got["target"].host)
        if hosts != ("src.local", "dst.local"):
            raise AssertionError(f"каждый параметр со своим профилем: {hosts}")


class TestRefusals:
    async def test_plain_reference_asks_to_seal(self) -> None:
        refused = await _refusal(SealKeys(), "conn://probe/main")

        if refused.kind != ConnectionRefusal.NOT_SEALED:
            raise AssertionError(f"kind отказа: {refused.kind}")
        if "connection_list" not in str(refused):
            raise AssertionError(f"подсказка повторить со ссылкой: {refused}")

    async def test_value_for_an_old_key_asks_for_a_new_one(self) -> None:
        previous = SealKeys().public()

        refused = await _refusal(SealKeys(), _sealed(previous, _probe("db.local")))

        if refused.kind != ConnectionRefusal.SEAL_KEY_UNKNOWN:
            raise AssertionError(f"kind отказа: {refused.kind}")
        if "current key of the server" not in str(refused):
            raise AssertionError(f"отказ велит клиенту взять новый ключ: {refused}")

    async def test_value_of_another_user_is_refused(self) -> None:
        keys = SealKeys()
        sealed = _sealed(keys.public(), _probe("db.local"), login="petrov")

        refused = await _refusal(keys, sealed)

        if refused.kind != ConnectionRefusal.SEALED_FOR_ANOTHER_USER:
            raise AssertionError(f"kind отказа: {refused.kind}")
        if "petrov" not in str(refused) or LOGIN not in str(refused):
            raise AssertionError(f"отказ называет обоих: {refused}")

    async def test_expired_value_is_refused(self) -> None:
        keys = SealKeys()
        sealed = _sealed(keys.public(), _probe("db.local"), ttl=timedelta(seconds=-1))

        refused = await _refusal(keys, sealed)

        if refused.kind != ConnectionRefusal.SEAL_EXPIRED:
            raise AssertionError(f"kind отказа: {refused.kind}")

    async def test_connection_of_another_kind_is_refused(self) -> None:
        keys = SealKeys()
        sealed = _sealed(keys.public(), OtherConnection(host="other.local"))

        refused = await _refusal(keys, sealed)

        if refused.kind != ConnectionRefusal.ANOTHER_KIND:
            raise AssertionError(f"kind отказа: {refused.kind}")
        if "'probe'" not in str(refused) or "'other'" not in str(refused):
            raise AssertionError(f"отказ называет оба вида: {refused}")


class TestDeclaredFeature:
    def test_feature_carries_the_key_a_client_can_seal_with(self) -> None:
        keys = SealKeys()
        params = SealedConnectionParams(keys, lambda: TYPES)
        params.bind_all([TOOLS.one_connection()])

        declared = SealFeature.model_validate(params.features()[SealFeature.ID])

        if declared.key != keys.public():
            raise AssertionError("возможность несёт открытый ключ исполнителя")

        opened = keys.open(_sealed(declared.key, _probe("db.local")))
        if opened.login != LOGIN:
            raise AssertionError(f"запечатанное ключом возможности открылось: {opened}")

    def test_server_without_connection_tools_declares_nothing(self) -> None:
        params = SealedConnectionParams(SealKeys(), lambda: TYPES)

        if params.features():
            raise AssertionError(f"возможности нет: {params.features()}")
