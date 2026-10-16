"""Путь соединения от имени в вызове модели до тела инструмента.

Клиент и исполнитель стоят в одном процессе (SealedStand): модель называет
соединение именем, клиент (SealingStand — то, что в бою делает
ретранслятор чата) по схеме инструмента узнаёт параметр и вид, запечатывает профиль
ключом исполнителя, исполнитель открывает его и отдаёт телу. Таблица
соединений подменена хранилищем в памяти.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from datetime import timedelta
from typing import Annotated, Literal

import pytest
from probe_stand import (
    LOGIN,
    SECRET,
    TYPES,
    OtherConnection,
    ProbeTools,
    SealedStand,
)

from boba.connection_broker.sealed import SealedConnectionParams
from boba.connections.base import ConnectionBase
from boba.connections.marks import ConnectionRefusal
from boba.connections.sealed import (
    ConnectionOffer,
    SealedParamSchema,
    SealFeature,
    SealKeys,
)
from boba.stand.sealing import SealingStand
from boba.stand_core.context import CallStand
from boba.toolkit.dag import DagNode, NodeOutcome, ToolCard, ToolServer
from boba.toolkit.facade import UserConnection
from boba.toolkit.result import ErrorResult, TableResult
from boba.toolrun.injected import StaticConfig, ToolConfigError

pytestmark = pytest.mark.anyio

MAIN = "main"


class _DeclaringAnotherKey(ToolServer):
    """Порт, который объявляет не те возможности, что исполнитель за ним:
    клиент подключался до перезапуска сервера и держит прежний ключ, либо
    сервер возможности не объявил вовсе."""

    def __init__(
        self, inner: ToolServer, features: Mapping[str, Mapping[str, object]]
    ) -> None:
        self._inner = inner
        self._features = features

    def tools(self) -> Sequence[ToolCard]:
        return self._inner.tools()

    def features(self) -> Mapping[str, Mapping[str, object]]:
        return self._features

    async def submit(
        self, calls: Sequence[DagNode]
    ) -> Sequence[asyncio.Future[NodeOutcome]]:
        return await self._inner.submit(calls)


def _stand() -> SealedStand:
    return SealedStand([SealedStand.probe_row("main", "db.local")])


def _row(message: NodeOutcome) -> dict[str, object]:
    result = message.artifact
    if not isinstance(result, TableResult):
        raise AssertionError(f"ожидалась таблица, пришло: {message.content}")

    return dict(result.rows[0])


def _refusal(message: NodeOutcome) -> ErrorResult:
    result = message.artifact
    if not isinstance(result, ErrorResult):
        raise AssertionError(f"ожидался отказ, пришло: {message.content}")
    if not message.errored:
        raise AssertionError(f"отказ помечен ошибкой: {message}")

    return result


class TestSchemaShownToTheModel:
    def test_server_declares_a_sealed_profile(self) -> None:
        stand = _stand()

        served = {card.name: card for card in stand.executor.tools()}
        declared = served["probe_query"].parameters["properties"]["connection"]

        if declared["type"] != "string":
            raise AssertionError(f"сервер ждёт строку: {declared}")
        if declared["contentMediaType"] != "application/jose":
            raise AssertionError(f"содержимое строки — JWE: {declared}")
        offers = SealedParamSchema().offers(served["probe_query"].parameters)
        if offers["connection"] != ConnectionOffer(kind="probe", sealed=True, names={}):
            raise AssertionError(
                f"схема предлагает запечатанный профиль вида: {offers}"
            )
        if "probe" not in declared["description"]:
            raise AssertionError(f"описание называет вид: {declared}")


class TestReferenceReachesTheBody:
    async def test_body_gets_the_profile_with_its_secret(self) -> None:
        stand = _stand()

        message = await stand.call("probe_query", {"connection": MAIN, "sql": "x"})

        row = _row(message)
        if row["host"] != "db.local":
            raise AssertionError(f"тело получило профиль строки main: {row}")
        if row["password"] != SECRET:
            raise AssertionError(f"секрет доехал до тела: {row}")

    async def test_profile_is_signed_by_the_caller(self) -> None:
        stand = _stand()

        message = await stand.call("probe_query", {"connection": MAIN, "sql": "x"})

        if _row(message)["client"] != LOGIN:
            raise AssertionError(f"профиль подписан вызывающим: {message.content}")

    async def test_two_parameters_resolve_independently(self) -> None:
        rows = [
            SealedStand.probe_row("left", "a.local"),
            SealedStand.probe_row("right", "b.local"),
        ]
        stand = SealedStand(rows)
        args = {"source": "left", "target": "right"}

        row = _row(await stand.call("probe_copy", args))

        if row != {"source": "a.local", "target": "b.local"}:
            raise AssertionError(f"каждый параметр получил своё соединение: {row}")

    async def test_call_of_the_model_keeps_the_reference(self) -> None:
        stand = _stand()
        args = {"connection": MAIN, "sql": "x"}

        await stand.call("probe_query", args)

        if args["connection"] != MAIN:
            raise AssertionError(f"аргументы модели не тронуты: {args}")


class TestKeyComesFromTheServerFeature:
    def test_client_passes_the_declared_feature_through(self) -> None:
        stand = _stand()

        declared = SealFeature.model_validate(stand.client.features()[SealFeature.ID])

        if declared.key != stand.keys.public():
            raise AssertionError("клиент видит ключ, объявленный исполнителем")

    async def test_server_without_the_feature_gets_no_connections(self) -> None:
        stand = _stand()
        silent = SealingStand(
            _DeclaringAnotherKey(stand.executor, {}),
            stand.connections,
            timedelta(minutes=10),
        )

        refusal = _refusal(
            await stand.call_through(
                silent, "probe_query", {"connection": MAIN, "sql": "x"}
            )
        )

        if refusal.error_kind != ConnectionRefusal.SEAL_KEY_UNKNOWN:
            raise AssertionError(f"соединение не отправлено: {refusal.message}")
        if SealFeature.ID not in refusal.message:
            raise AssertionError(f"отказ называет возможность: {refusal.message}")

    async def test_value_sealed_for_a_restarted_server_is_refused(self) -> None:
        stand = _stand()
        restarted = SealedStand([])
        stale = SealingStand(
            _DeclaringAnotherKey(stand.executor, restarted.params.features()),
            stand.connections,
            timedelta(minutes=10),
        )

        message = await stand.call_through(
            stale, "probe_query", {"connection": MAIN, "sql": "x"}
        )

        refusal = _refusal(message)
        if refusal.error_kind != ConnectionRefusal.SEAL_KEY_UNKNOWN:
            raise AssertionError(f"чужой ключ отвергнут сервером: {refusal}")


class TestRefusals:
    """Отказ запечатывания — отказ клиента до отправки серверу: в бою его
    возвращает хук, порт стенда отдаёт его итогом-отказом вызова."""

    async def test_unknown_name_is_refused_with_the_available_ones(self) -> None:
        stand = _stand()
        args = {"connection": "нет-такого", "sql": "x"}

        refusal = _refusal(await stand.call("probe_query", args))

        if refusal.error_kind != ConnectionRefusal.NOT_VISIBLE:
            raise AssertionError(f"чужое имя отвергнуто: {refusal.message}")
        if "main" not in refusal.message:
            raise AssertionError(f"отказ называет доступные: {refusal.message}")

    async def test_duplicate_name_is_refused(self) -> None:
        rows = [
            SealedStand.probe_row("dup", "a.local"),
            SealedStand.probe_row("dup", "b.local"),
        ]
        stand = SealedStand(rows)
        args = {"connection": "dup", "sql": "x"}

        refusal = _refusal(await stand.call("probe_query", args))

        if refusal.error_kind != ConnectionRefusal.AMBIGUOUS:
            raise AssertionError(f"дубль имени отвергнут: {refusal.message}")

    async def test_name_of_another_kind_is_not_visible(self) -> None:
        """Вид параметра берётся из схемы инструмента: строка другого вида с
        таким именем параметру не видна."""
        stand = SealedStand([SealedStand.row("web", OtherConnection(host="h"))])
        args = {"connection": "web", "sql": "x"}

        refusal = _refusal(await stand.call("probe_query", args))

        if refusal.error_kind != ConnectionRefusal.NOT_VISIBLE:
            raise AssertionError(f"строка другого вида не видна: {refusal.message}")

    async def test_plain_value_is_refused_by_the_client_without_echo(self) -> None:
        """Вместо имени модель написала строку подключения с паролем: клиент
        отвергает её как неизвестное имя, не цитируя, и серверу не шлёт."""
        stand = _stand()
        dsn = f"postgres://user:{SECRET}@db.local/x"

        refusal = _refusal(
            await stand.call("probe_query", {"connection": dsn, "sql": "x"})
        )

        if refusal.error_kind != ConnectionRefusal.NOT_VISIBLE:
            raise AssertionError(f"неизвестное имя отвергнуто: {refusal.message}")
        if SECRET in refusal.message:
            raise AssertionError(f"значение не цитируется: {refusal.message}")
        if "main" not in refusal.message:
            raise AssertionError(f"отказ называет доступные: {refusal.message}")


class TestDeclarationIsChecked:
    def test_parameter_must_be_a_connection_model(self, call_stand: CallStand) -> None:
        tool = ProbeTools().tool(
            "broken", {"connection": (Annotated[str, UserConnection], ...)}
        )

        with pytest.raises(ToolConfigError, match="not a connection model"):
            SealedConnectionParams(
                SealKeys(), lambda: TYPES, call_stand.contexts, {}, StaticConfig()
            ).bind_all([tool])

    def test_type_package_must_be_installed(self, call_stand: CallStand) -> None:
        class Unregistered(ConnectionBase):
            kind: Literal["unregistered"] = "unregistered"

            def trace(self) -> str:
                return "unregistered"

        tool = ProbeTools().tool(
            "broken", {"connection": (Annotated[Unregistered, UserConnection], ...)}
        )

        with pytest.raises(ToolConfigError, match="not installed"):
            SealedConnectionParams(
                SealKeys(), lambda: TYPES, call_stand.contexts, {}, StaticConfig()
            ).bind_all([tool])
