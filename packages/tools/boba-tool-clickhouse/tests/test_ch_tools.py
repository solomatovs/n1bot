"""Обвязка ClickHouse-инструментов: имена, whitelist, ошибки, конфиг, SPNEGO."""

from __future__ import annotations

from typing import Any, ClassVar

import pytest
from pydantic import ValidationError

from boba.db.clickhouse import ClickHouseConfig
from boba.db.clickhouse.payload import PayloadClickHouse, SpnegoHeaders
from boba.tool.ch import ChExecutorConfig, build_ch_tools
from boba.tool.ch import executor as ch_executor
from boba.toolkit.result import ErrorResult, TableResult, ToolArtifact


class _NoLauncher:
    """Исполнитель-заглушка: тесты проверяют обвязку, песочница им не нужна."""

    def call_text(self, command: str, stdin: str) -> Any:
        raise AssertionError("песочница не должна вызываться")

    def call_stream(self, entry: Any, request: Any, sink: Any, trailer: Any) -> Any:
        raise AssertionError("песочница не должна вызываться")


def _no_launcher(tool: str) -> Any:
    return _NoLauncher()


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
