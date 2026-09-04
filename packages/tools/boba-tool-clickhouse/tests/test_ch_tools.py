"""ClickHouse-инструменты: состав, параметр-соединение, ошибки, SPNEGO."""

from __future__ import annotations

from typing import Any, ClassVar

import pytest
from pydantic import ValidationError

from boba.db.clickhouse.payload import SpnegoHeaders
from boba.db.clickhouse.profile import ClickHouseConfig
from boba.tool.ch.tools import TOOLS as CH_TOOLS
from boba.tool.ch.tools import ChToolConfig
from boba.toolkit.entry import ToolArgv
from boba.toolkit.facade import PayloadTool


def ch_config() -> ChToolConfig:
    return ChToolConfig.model_validate({"max_rows": 10})


class TestChTools:
    _NAMES: ClassVar[list[str]] = [
        "ch_list_tables",
        "ch_describe_table",
        "ch_query",
    ]

    def test_module_declares_the_toolset(self) -> None:
        names = [t.name for t in CH_TOOLS]
        if names != self._NAMES:
            raise AssertionError(f"names == self._NAMES, got {names}")

    def test_every_tool_takes_a_connection_parameter(self) -> None:
        """Профиль подаёт хост: у каждого инструмента параметр с маркером."""
        for payload in CH_TOOLS:
            if not isinstance(payload, PayloadTool):
                raise AssertionError(f"{payload} is not a PayloadTool")

            fields = ToolArgv.connection_fields(payload.args_schema)
            if list(fields) != ["connection"]:
                raise AssertionError(f"{payload.name}: connection parameter is missing")

            if fields["connection"] is not ClickHouseConfig:
                raise AssertionError(f"{payload.name}: profile type must be declared")

    def test_section_config_holds_limits_only(self) -> None:
        """Whitelist ушёл на хост: в секции остались только границы выдачи."""
        cfg = ch_config()
        if cfg.max_rows != 10:
            raise AssertionError("section keys must reach the model")

        if hasattr(cfg, "profiles"):
            raise AssertionError("profiles must not live in the section any more")


class TestClickHouseConfig:
    _KERBEROS: ClassVar[dict[str, str]] = {
        "method": "kerberos_keytab",
        "principal": "svc@EXAMPLE.COM",
        "keytab": "/etc/boba/krb5.keytab",
        "service": "HTTP",
    }
    _PASSWORD: ClassVar[dict[str, str]] = {
        "method": "password",
        "user": "u",
        "password": "s3cret",
    }
    _BASE: ClassVar[dict[str, Any]] = {
        "host": "ch",
        "port": 8123,
        "interface": "http",
    }

    def test_client_settings_drop_none_and_reveal_password(self) -> None:
        config = ClickHouseConfig.model_validate({**self._BASE, "auth": self._PASSWORD})
        settings = config.client_settings()
        if settings["username"] != "u":
            raise AssertionError('settings["username"] == "u"')
        if settings["password"] != "s3cret":
            raise AssertionError('settings["password"] == "s3cret"')
        if "ca_cert" in settings:
            raise AssertionError('"ca_cert" not in settings')
        if settings["settings"] != {}:
            raise AssertionError('settings["settings"] == {}')

    def test_password_is_masked_without_reveal_context(self) -> None:
        config = ClickHouseConfig.model_validate({**self._BASE, "auth": self._PASSWORD})
        dumped = config.model_dump(mode="json")["auth"]
        if dumped["password"] is not None:
            raise AssertionError('dumped["password"] is None')
        revealed = config.model_dump(
            mode="json", context={ClickHouseConfig.REVEAL_SECRETS: True}
        )
        if revealed["auth"]["password"] != "s3cret":
            raise AssertionError('revealed["auth"]["password"] == "s3cret"')

    def test_auth_is_required(self) -> None:
        with pytest.raises(ValidationError, match="auth"):
            ClickHouseConfig.model_validate(self._BASE)

    def test_kerberos_requires_connect_timeout(self) -> None:
        with pytest.raises(ValidationError, match="connect_timeout"):
            ClickHouseConfig.model_validate({**self._BASE, "auth": self._KERBEROS})

    def test_no_password_auth_sends_only_username(self) -> None:
        config = ClickHouseConfig.model_validate(
            {**self._BASE, "auth": {"method": "no_password", "user": "u"}}
        )
        settings = config.client_settings()
        if settings["username"] != "u":
            raise AssertionError('settings["username"] == "u"')
        if "password" in settings:
            raise AssertionError('"password" not in settings')

    def test_kerberos_sends_no_username(self) -> None:
        config = ClickHouseConfig.model_validate(
            {**self._BASE, "auth": self._KERBEROS, "connect_timeout": 5}
        )
        settings = config.client_settings()
        if "username" in settings or "password" in settings:
            raise AssertionError("kerberos must not send basic credentials")

    def test_service_name_prefers_server_host_name(self) -> None:
        config = ClickHouseConfig.model_validate(
            {
                **self._BASE,
                "host": "10.0.0.50",
                "server_host_name": "ch01.example.com",
                "auth": self._KERBEROS,
                "connect_timeout": 5,
            }
        )
        if config.service_name() != "HTTP@ch01.example.com":
            raise AssertionError('config.service_name() == "HTTP@ch01.example.com"')


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
        if first["Authorization"] != "Negotiate token-1":
            raise AssertionError('first["Authorization"] == "Negotiate token-1"')
        if second["Authorization"] != "Negotiate token-2":
            raise AssertionError('second["Authorization"] == "Negotiate token-2"')
        if first["User-Agent"] != "boba":
            raise AssertionError('first["User-Agent"] == "boba"')

    def test_stored_headers_keep_no_token(self) -> None:
        headers = self._Fake()
        headers.copy()
        if SpnegoHeaders.HEADER in headers:
            raise AssertionError("SpnegoHeaders.HEADER not in headers")


class TestJsonable:
    def test_row_becomes_json_safe_mapping(self) -> None:
        from datetime import date
        from decimal import Decimal
        from uuid import UUID

        from boba.toolkit.launcher import RowStream

        row = {
            "i": 1,
            "d": Decimal("1.5"),
            "u": UUID("00000000-0000-0000-0000-000000000001"),
            "dt": date(2026, 1, 2),
            "arr": (1, 2),
            "map": {"k": b"v"},
            "empty": None,
        }
        if not (
            RowStream.plain(row)
            == {
                "i": 1,
                "d": "1.5",
                "u": "00000000-0000-0000-0000-000000000001",
                "dt": "2026-01-02",
                "arr": [1, 2],
                "map": {"k": "v"},
                "empty": None,
            }
        ):
            raise AssertionError('RowStream.plain(row) == { "i": 1, "d": "1.5", "u": …')
