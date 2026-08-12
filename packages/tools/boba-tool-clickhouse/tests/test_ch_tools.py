"""Обвязка ClickHouse-инструментов: узлы, whitelist, ошибки, конфиг, SPNEGO."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, ClassVar

import pytest
from pydantic import ValidationError

from boba.db.clickhouse import ClickHouseConfig
from boba.db.clickhouse.payload import PayloadClickHouse, SpnegoHeaders
from boba.tool.ch import ChExecutorConfig, build_ch_tools
from boba.tool.ch import executor as ch_executor
from boba.tool.ch.caller import ChCaller
from boba.tool.ch.protocol import (
    ChInsertRequest,
    ChQueryRequest,
    ChStage,
    ChWireFormat,
)
from boba.tool.ch.stages import ChInsertNode, ChQueryNode
from boba.toolkit.channels import ChannelSink, StreamCodec, StreamFormat, StreamKey
from boba.toolkit.launcher import ChannelHead
from boba.toolkit.result import ErrorResult, TableResult, ToolArtifact
from boba.toolkit.workflow import WorkflowOutcome, WorkflowSpec


class _NoLauncher:
    """Исполнитель-заглушка: тесты проверяют обвязку, песочница им не нужна."""

    def call(
        self,
        spec: WorkflowSpec,
        sinks: Mapping[str, ChannelSink] | None = None,
    ) -> WorkflowOutcome:
        raise AssertionError("песочница не должна вызываться")


def _no_launcher(tool: str) -> Any:
    return _NoLauncher()


class _RowsLauncher:
    """Исполнитель графа из одного узла: строки в приёмник, квитанция в итог."""

    def __init__(self, rows: list[dict[str, Any]], truncated: bool) -> None:
        self._rows = rows
        self._truncated = truncated
        self.spec: WorkflowSpec | None = None

    def call(
        self,
        spec: WorkflowSpec,
        sinks: Mapping[str, ChannelSink] | None = None,
    ) -> WorkflowOutcome:
        self.spec = spec
        stage = spec.nodes[0].id

        if sinks is not None:
            sink = sinks[stage]
            for row in self._rows:
                sink.feed(StreamCodec.encode_row(row))
            sink.close()

        return WorkflowOutcome(
            stages=(),
            trailers={stage: {"truncated": self._truncated}},
        )

    def head(self, key: StreamKey, max_bytes: int) -> ChannelHead:
        """Журнала у тестового исполнителя нет: голова канала пуста."""
        return ChannelHead.empty()


def ch_config() -> ChExecutorConfig:
    return ChExecutorConfig.model_validate(
        {
            "profiles": {
                "main": {
                    "host": "h",
                    "port": 8123,
                    "interface": "http",
                    "username": "u",
                }
            }
        }
    )


def invoke(tool: Any, args: dict[str, Any]) -> Any:
    message = tool.invoke(
        {"name": tool.name, "args": args, "id": "c1", "type": "tool_call"}
    )
    return ToolArtifact.revive(message.artifact)


class TestChTools:
    _TARGET_ARG: ClassVar[str] = "connection_name"

    def test_all_four_are_built(self) -> None:
        names = [t.name for t in build_ch_tools(ch_config(), _no_launcher)]
        assert names == [
            "ch_list_targets",
            "ch_list_tables",
            "ch_describe_table",
            "ch_query",
        ]

    def test_list_targets_returns_whitelist(self) -> None:
        tool = build_ch_tools(ch_config(), _no_launcher)[0]
        result = invoke(tool, {})
        assert isinstance(result, TableResult)
        assert list(result.rows) == [{"connection_name": "main"}]
        assert result.ok is True

    def test_unknown_target_becomes_error_result(self) -> None:
        """Профиль не в whitelist — ошибка инструмента, а не падение хода."""
        for name in ("ch_list_tables", "ch_describe_table", "ch_query"):
            built = build_ch_tools(ch_config(), _no_launcher)
            tool = next(t for t in built if t.name == name)
            args: dict[str, Any] = {self._TARGET_ARG: "нет-такого"}
            if name == "ch_describe_table":
                args["table"] = "t"
            if name == "ch_query":
                args["sql"] = "select 1"
            result = invoke(tool, args)
            assert isinstance(result, ErrorResult), name
            assert result.error_kind == "unknown_target", name
            assert result.ok is False, name

    def test_sql_error_becomes_error_result(self, monkeypatch) -> None:
        def boom(*_args: Any, **_kwargs: Any):
            raise ch_executor.ChQueryError("unknown table")

        monkeypatch.setattr(ch_executor.ChExecutor, "execute", boom)
        built = build_ch_tools(ch_config(), _no_launcher)
        tool = next(t for t in built if t.name == "ch_list_tables")
        result = invoke(tool, {"connection_name": "main"})
        assert isinstance(result, ErrorResult)
        assert result.ok is False
        assert "unknown table" in result.message

    def test_profiles_are_required(self) -> None:
        with pytest.raises(ValidationError, match="no profiles configured"):
            ChExecutorConfig.model_validate({"profiles": {}})

    def test_query_reaches_launcher_as_single_node_graph(self) -> None:
        """Фасад строит вырожденный граф; строки приезжают приёмником узла."""
        launcher = _RowsLauncher([{"a": 1}], truncated=False)
        built = build_ch_tools(ch_config(), lambda tool: launcher)
        tool = next(t for t in built if t.name == "ch_query")

        result = invoke(tool, {"connection_name": "main", "sql": "select 1"})

        assert isinstance(result, TableResult)
        assert list(result.rows) == [{"a": 1}]

        spec = launcher.spec
        assert spec is not None
        assert (spec.nodes[0].id, spec.nodes[0].tool) == (
            ChStage.QUERY,
            ChStage.QUERY,
        )
        assert spec.nodes[0].args["connection_name"] == "main"

    def test_graph_spec_carries_no_connection_profile(self) -> None:
        """Спека графа сериализуема и показываема: секретов в args нет."""
        launcher = _RowsLauncher([], truncated=False)
        caller = ChCaller("ch", lambda tool: launcher)

        caller.query(
            connection_name="main",
            sql="select 1",
            params={},
            sink=_Discard(),
        )

        spec = launcher.spec
        assert spec is not None
        assert set(spec.nodes[0].args) == {"connection_name", "sql", "params"}


class _Discard(ChannelSink):
    """Приёмник, которому данные не нужны."""

    def feed(self, data: bytes) -> None:
        """Байты никуда не идут: тест смотрит на спеку, а не на поток."""

    def close(self) -> None:
        """Закрытие приёмника ничего не освобождает."""


class TestChNodes:
    """Обогатители узлов: профиль и лимиты из конфига, секрет в запросе payload'а."""

    @staticmethod
    def _config() -> ChExecutorConfig:
        return ChExecutorConfig.model_validate(
            {
                "profiles": {
                    "main": {
                        "host": "h",
                        "port": 8123,
                        "interface": "http",
                        "username": "u",
                        "password": "s3cret",
                    }
                },
                "max_rows": 7,
            }
        )

    def test_query_enricher_builds_read_only_request(self) -> None:
        enrich = ChQueryNode.enricher(self._config())

        enriched = enrich({"connection_name": "main", "sql": "select 1"})
        request = ChQueryRequest.model_validate(enriched)

        assert request.op == ChStage.QUERY
        assert request.row_limit == 7
        assert request.connection.settings.readonly == ClickHouseConfig.READ_ONLY
        assert request.connection.password is not None
        assert request.connection.password.get_secret_value() == "s3cret"

    def test_insert_enricher_keeps_writable_session(self) -> None:
        enrich = ChInsertNode.enricher(self._config())

        enriched = enrich(
            {
                "connection_name": "main",
                "table": "events",
                "stdin_format": StreamFormat.NDJSON.value,
            }
        )
        request = ChInsertRequest.model_validate(enriched)

        assert request.table == "events"
        assert request.stdin_format is StreamFormat.NDJSON
        assert request.connection.settings.readonly is None

    def test_unknown_connection_is_rejected_before_the_stage(self) -> None:
        enrich = ChQueryNode.enricher(self._config())

        with pytest.raises(ValueError, match="whitelist"):
            enrich({"connection_name": "нет-такого", "sql": "select 1"})

    def test_insert_declares_no_product_and_takes_row_formats(self) -> None:
        assert ChInsertNode.CONTRACT.out is None

        with pytest.raises(ValidationError, match="not insertable"):
            ChInsertRequest.model_validate(
                {
                    "op": ChStage.INSERT,
                    "connection": {
                        "host": "h",
                        "port": 8123,
                        "interface": "http",
                        "username": "u",
                    },
                    "table": "events",
                    "stdin_format": StreamFormat.TEXT,
                }
            )

    def test_wire_format_follows_the_declared_input(self) -> None:
        assert ChWireFormat.of(StreamFormat.NDJSON) is ChWireFormat.JSON_EACH_ROW
        assert ChWireFormat.of(StreamFormat.CSV) is ChWireFormat.CSV_WITH_NAMES

        with pytest.raises(ValueError, match="not insertable"):
            ChWireFormat.of(StreamFormat.BYTES)


class TestClickHouseConfig:
    _KERBEROS: ClassVar[dict[str, str]] = {
        "keytab": "/etc/boba/krb5.keytab",
        "principal": "boba-svc@LOSHARA.COM",
        "ccache": "FILE:/tmp/krb5cc_boba_ch",
    }
    _BASE: ClassVar[dict[str, Any]] = {
        "host": "ch",
        "port": 8123,
        "interface": "http",
    }

    def test_read_only_switches_session(self) -> None:
        config = ClickHouseConfig.model_validate({**self._BASE, "username": "u"})
        assert config.settings.readonly is None
        assert config.read_only().settings.readonly == ClickHouseConfig.READ_ONLY

    def test_client_settings_drop_none_and_reveal_password(self) -> None:
        config = ClickHouseConfig.model_validate(
            {**self._BASE, "username": "u", "password": "s3cret"}
        )
        settings = config.client_settings()
        assert settings["password"] == "s3cret"
        assert "ca_cert" not in settings
        assert settings["settings"] == {}

    def test_password_is_masked_without_reveal_context(self) -> None:
        config = ClickHouseConfig.model_validate(
            {**self._BASE, "username": "u", "password": "s3cret"}
        )
        assert config.model_dump(mode="json")["password"] is None
        revealed = config.model_dump(
            mode="json", context={ClickHouseConfig.REVEAL_SECRETS: True}
        )
        assert revealed["password"] == "s3cret"

    def test_username_is_required_without_kerberos(self) -> None:
        with pytest.raises(ValidationError, match="username обязателен"):
            ClickHouseConfig.model_validate(self._BASE)

    def test_kerberos_requires_krbsrvname(self) -> None:
        with pytest.raises(ValidationError, match="krbsrvname"):
            ClickHouseConfig.model_validate({**self._BASE, "kerberos": self._KERBEROS})

    def test_kerberos_and_password_are_exclusive(self) -> None:
        with pytest.raises(ValidationError, match="взаимоисключающи"):
            ClickHouseConfig.model_validate(
                {
                    **self._BASE,
                    "kerberos": self._KERBEROS,
                    "krbsrvname": "HTTP",
                    "password": "s3cret",
                }
            )

    def test_service_name_prefers_server_host_name(self) -> None:
        config = ClickHouseConfig.model_validate(
            {
                **self._BASE,
                "host": "172.18.0.50",
                "server_host_name": "ch01.loshara.com",
                "kerberos": self._KERBEROS,
                "krbsrvname": "HTTP",
            }
        )
        assert config.service_name() == "HTTP@ch01.loshara.com"


class TestSpnegoHeaders:
    """Заголовок обязан пересобираться на каждый запрос: replay сервер не примет."""

    class _Fake(SpnegoHeaders):
        def __init__(self) -> None:
            super().__init__("HTTP@ch")
            self.issued = 0

        def _negotiate(self) -> str:
            self.issued += 1
            return f"Negotiate token-{self.issued}"

    def test_copy_issues_new_token_each_time(self) -> None:
        headers = self._Fake()
        headers["User-Agent"] = "boba"
        first, second = headers.copy(), headers.copy()
        assert first["Authorization"] == "Negotiate token-1"
        assert second["Authorization"] == "Negotiate token-2"
        assert first["User-Agent"] == "boba"

    def test_stored_headers_keep_no_token(self) -> None:
        headers = self._Fake()
        headers.copy()
        assert SpnegoHeaders.HEADER not in headers


class TestJsonable:
    def test_row_becomes_json_safe_mapping(self) -> None:
        from datetime import date
        from decimal import Decimal
        from uuid import UUID

        row = (
            1,
            Decimal("1.5"),
            UUID("00000000-0000-0000-0000-000000000001"),
            date(2026, 1, 2),
            (1, 2),
            {"k": b"v"},
            None,
        )
        names = ("i", "d", "u", "dt", "arr", "map", "empty")
        assert PayloadClickHouse.jsonable(names, row) == {
            "i": 1,
            "d": "1.5",
            "u": "00000000-0000-0000-0000-000000000001",
            "dt": "2026-01-02",
            "arr": [1, 2],
            "map": {"k": "v"},
            "empty": None,
        }
