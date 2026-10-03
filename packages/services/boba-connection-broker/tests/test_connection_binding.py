"""Путь соединения от ссылки модели до тела инструмента.

Клиент и исполнитель стоят в одном процессе (SealedStand): модель называет
соединение ссылкой, клиент запечатывает профиль ключом исполнителя,
исполнитель открывает его и отдаёт телу. Таблица соединений подменена
хранилищем в памяти.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from datetime import timedelta
from typing import Annotated, Literal

import pytest
from langchain_core.messages import ToolCall, ToolMessage
from langchain_core.tools import BaseTool
from probe_stand import (
    LOGIN,
    SECRET,
    TYPES,
    OtherConnection,
    ProbeTools,
    SealedStand,
)

from boba.connection_broker.sealed import SealedConnectionParams
from boba.connection_broker.sealing import SealingToolServer
from boba.connections.base import ConnectionBase
from boba.connections.marks import ConnectionRefusal
from boba.connections.sealed import ConnectionRef, SealFeature, SealKeys
from boba.stand_core.context import CallStand
from boba.toolkit.facade import UserConnection
from boba.toolkit.result import ErrorResult, TableResult, ToolArtifact
from boba.toolrun.injected import ToolConfigError
from boba.toolrun.stream_calls import ToolServer, WorkflowTool
from boba.toolrun.wrapping import ToolSchema

pytestmark = pytest.mark.anyio

MAIN = ConnectionRef(kind="probe", name="main").render()


class _DeclaringAnotherKey(ToolServer):
    """Порт, который объявляет не те возможности, что исполнитель за ним:
    клиент подключался до перезапуска сервера и держит прежний ключ, либо
    сервер возможности не объявил вовсе."""

    def __init__(
        self, inner: ToolServer, features: Mapping[str, Mapping[str, object]]
    ) -> None:
        self._inner = inner
        self._features = features

    def tools(self) -> Sequence[BaseTool]:
        return self._inner.tools()

    def features(self) -> Mapping[str, Mapping[str, object]]:
        return self._features

    async def submit(
        self, calls: Sequence[ToolCall]
    ) -> Sequence[asyncio.Future[ToolMessage]]:
        return await self._inner.submit(calls)


class _Recorder(_DeclaringAnotherKey):
    """Порт с заданными возможностями, запоминающий вызовы, которые до него
    дошли."""

    def __init__(
        self, inner: ToolServer, features: Mapping[str, Mapping[str, object]]
    ) -> None:
        super().__init__(inner, features)
        self.calls: list[ToolCall] = []

    async def submit(
        self, calls: Sequence[ToolCall]
    ) -> Sequence[asyncio.Future[ToolMessage]]:
        self.calls.extend(calls)

        return await super().submit(calls)


def _stand() -> SealedStand:
    return SealedStand([SealedStand.probe_row("main", "db.local")])


def _row(message: ToolMessage) -> dict[str, object]:
    result = ToolArtifact.revive(message.artifact)
    if not isinstance(result, TableResult):
        raise AssertionError(f"ожидалась таблица, пришло: {message.content}")

    return dict(result.rows[0])


def _refusal(message: ToolMessage) -> ErrorResult:
    result = ToolArtifact.revive(message.artifact)
    if not isinstance(result, ErrorResult):
        raise AssertionError(f"ожидался отказ, пришло: {message.content}")
    if message.status != "error":
        raise AssertionError(f"отказ помечен ошибкой: {message.status}")

    return result


class TestSchemaShownToTheModel:
    def test_connection_parameter_is_a_marked_string(self) -> None:
        stand = _stand()

        offered = {tool.name: tool for tool in stand.client.tools()}
        shown = ToolSchema.of(offered["probe_query"])
        if shown is None:
            raise AssertionError("схема инструмента пропала")

        declared = shown.model_json_schema()["properties"]["connection"]

        if declared["type"] != "string":
            raise AssertionError(f"модель видит строку: {declared}")
        if declared[ConnectionRef.SCHEMA_MARK] != "probe":
            raise AssertionError(f"параметр помечен видом соединения: {declared}")
        if "connection_list" not in declared["description"]:
            raise AssertionError(f"описание ведёт к каталогу: {declared}")


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
        args = {"source": "conn://probe/left", "target": "conn://probe/right"}

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
        silent = SealingToolServer(
            _DeclaringAnotherKey(stand.executor, {}),
            stand.connections,
            stand.sent,
            timedelta(minutes=10),
        )

        message = await stand.call_through(
            silent, "probe_query", {"connection": MAIN, "sql": "x"}
        )

        refusal = _refusal(message)
        if refusal.error_kind != ConnectionRefusal.SEAL_KEY_UNKNOWN:
            raise AssertionError(f"соединение не отправлено: {refusal}")
        if SealFeature.ID not in refusal.message:
            raise AssertionError(f"отказ называет возможность: {refusal.message}")

    async def test_value_sealed_for_a_restarted_server_is_refused(self) -> None:
        stand = _stand()
        restarted = SealedStand([])
        stale = SealingToolServer(
            _DeclaringAnotherKey(stand.executor, restarted.params.features()),
            stand.connections,
            stand.sent,
            timedelta(minutes=10),
        )

        message = await stand.call_through(
            stale, "probe_query", {"connection": MAIN, "sql": "x"}
        )

        refusal = _refusal(message)
        if refusal.error_kind != ConnectionRefusal.SEAL_KEY_UNKNOWN:
            raise AssertionError(f"чужой ключ отвергнут сервером: {refusal}")


class TestWorkflowNodes:
    """Связка одним вызовом: соединения лежат в аргументах узлов, каждый узел —
    вызов своего инструмента."""

    async def test_references_inside_nodes_are_sealed_by_the_tool_of_the_node(
        self,
    ) -> None:
        stand = _stand()
        features = {
            **stand.params.features(),
            WorkflowTool.FEATURE: {"tool": WorkflowTool.NAME},
        }
        recorder = _Recorder(stand.executor, features)
        client = SealingToolServer(
            recorder, stand.connections, stand.sent, timedelta(minutes=10)
        )
        nodes = [
            {
                "key": "q",
                "tool": "probe_query",
                "args": {"connection": MAIN, "sql": "x"},
            }
        ]

        await stand.call_through(client, WorkflowTool.NAME, {"nodes": nodes})

        sent = recorder.calls[0]["args"]["nodes"][0]["args"]
        opened = stand.keys.open(sent["connection"])
        if opened.profile["host"] != "db.local":
            raise AssertionError(f"узлу ушло запечатанное соединение: {opened}")
        if sent["sql"] != "x":
            raise AssertionError(f"остальные аргументы узла как есть: {sent}")
        if nodes[0]["args"]["connection"] != MAIN:
            raise AssertionError(f"вызов модели не тронут: {nodes}")


class TestRefusals:
    async def test_unknown_name_is_refused_with_the_available_ones(self) -> None:
        stand = _stand()
        args = {"connection": "conn://probe/нет-такого", "sql": "x"}

        refusal = _refusal(await stand.call("probe_query", args))

        if refusal.error_kind != ConnectionRefusal.NOT_VISIBLE:
            raise AssertionError(f"чужое имя отвергнуто: {refusal}")
        if "main" not in refusal.message:
            raise AssertionError(f"отказ называет доступные: {refusal.message}")

    async def test_duplicate_name_is_refused(self) -> None:
        rows = [
            SealedStand.probe_row("dup", "a.local"),
            SealedStand.probe_row("dup", "b.local"),
        ]
        stand = SealedStand(rows)
        args = {"connection": "conn://probe/dup", "sql": "x"}

        refusal = _refusal(await stand.call("probe_query", args))

        if refusal.error_kind != ConnectionRefusal.AMBIGUOUS:
            raise AssertionError(f"дубль имени отвергнут: {refusal}")

    async def test_reference_of_another_kind_is_refused_before_arming(self) -> None:
        stand = SealedStand([SealedStand.row("web", OtherConnection(host="h"))])
        args = {"connection": "conn://other/web", "sql": "x"}

        refusal = _refusal(await stand.call("probe_query", args))

        if refusal.error_kind != ConnectionRefusal.ANOTHER_KIND:
            raise AssertionError(f"вид сверен по метке схемы: {refusal}")

    async def test_broken_reference_is_refused(self) -> None:
        stand = _stand()

        refusal = _refusal(
            await stand.call("probe_query", {"connection": "conn://main", "sql": "x"})
        )

        if refusal.error_kind != ConnectionRefusal.NOT_VISIBLE:
            raise AssertionError(f"ссылка без вида отвергнута: {refusal}")

    async def test_plain_value_is_refused_by_the_server_without_echo(self) -> None:
        stand = _stand()
        dsn = f"postgres://user:{SECRET}@db.local/x"

        refusal = _refusal(
            await stand.call("probe_query", {"connection": dsn, "sql": "x"})
        )

        if refusal.error_kind != ConnectionRefusal.NOT_SEALED:
            raise AssertionError(f"незапечатанное значение отвергнуто: {refusal}")
        if SECRET in refusal.message:
            raise AssertionError(f"значение не цитируется: {refusal.message}")


class TestShownToTheUser:
    """Лента клиента рисует шаг по аргументам, с которыми тело вызвано; на
    месте запечатанного значения она показывает ссылку модели."""

    async def test_sealed_value_is_shown_as_the_reference_while_the_call_runs(
        self,
    ) -> None:
        stand = _stand()
        recorder = _Recorder(stand.executor, stand.params.features())
        client = SealingToolServer(
            recorder, stand.connections, stand.sent, timedelta(minutes=10)
        )
        call = ToolCall(
            name="probe_query",
            args={"connection": MAIN, "sql": "x"},
            id="call_shown",
            type="tool_call",
        )

        with stand.as_caller():
            pending = await client.submit([call])
            running = stand.sent.shown(recorder.calls[0]["args"])
            await pending[0]

        if running["connection"] != MAIN:
            raise AssertionError(f"во время вызова показана ссылка: {running}")

        after = stand.sent.shown(recorder.calls[0]["args"])
        if after["connection"] == MAIN:
            raise AssertionError("после конца вызова пара забыта")


class TestExecutorRule:
    def test_sealed_values_are_not_shown_back(self) -> None:
        stand = _stand()

        shown = stand.params.shown(
            "probe_query", {"connection": "eyJ.a.b.c.d", "sql": "x"}
        )

        if "eyJ" in str(shown["connection"]):
            raise AssertionError(f"запечатанное значение скрыто: {shown}")
        if shown["sql"] != "x":
            raise AssertionError(f"остальные аргументы как есть: {shown}")

    def test_tool_without_connections_is_left_alone(self) -> None:
        args = {"connection": "x"}

        if _stand().params.shown("workflow", args) is not args:
            raise AssertionError("инструмент без соединений не трогается")


class TestDeclarationIsChecked:
    def test_parameter_must_be_a_connection_model(self, call_stand: CallStand) -> None:
        tool = ProbeTools().tool(
            "broken", {"connection": (Annotated[str, UserConnection], ...)}
        )

        with pytest.raises(ToolConfigError, match="not a connection model"):
            SealedConnectionParams(
                SealKeys(), lambda: TYPES, call_stand.contexts
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
                SealKeys(), lambda: TYPES, call_stand.contexts
            ).bind_all([tool])
