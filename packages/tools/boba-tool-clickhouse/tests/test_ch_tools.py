"""ClickHouse-инструменты новой модели: состав, whitelist, ошибки, SPNEGO."""

from __future__ import annotations

from typing import Any, ClassVar

import pytest
from pydantic import ValidationError

from boba.db.clickhouse import ClickHouseConfig
from boba.db.clickhouse.payload import SpnegoHeaders
from boba.tool.ch.tools import TOOLS as CH_TOOLS
from boba.tool.ch.tools import ChToolConfig, ch_list_targets, ch_query
from boba.toolkit.entry import ExpectedErrors, ToolMain
from boba.toolkit.result import TableResult
from boba.toolkit.sql import UnknownConnectionError


def ch_config() -> ChToolConfig:
    return ChToolConfig.model_validate(
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


@pytest.mark.anyio
class TestChTools:
    _NAMES: ClassVar[list[str]] = [
        "ch_list_targets",
        "ch_list_tables",
        "ch_describe_table",
        "ch_query",
    ]

    def test_module_declares_the_toolset(self) -> None:
        names = [t.name for t in CH_TOOLS]
        if names != self._NAMES:
            raise AssertionError("names == self._NAMES")

    async def test_list_targets_returns_whitelist(self) -> None:
        body = ToolMain.toolset(ch_list_targets)[0].coroutine
        if body is None:
            raise AssertionError("body is not None")
        _content, artifact = await body(cfg=ch_config())

        if not (isinstance(artifact, TableResult)):
            raise AssertionError("isinstance(artifact, TableResult)")
        if list(artifact.rows) != [{"connection_name": "main"}]:
            raise AssertionError('list(artifact.rows) == [{"connection_name": "main"}]')
        if artifact.ok is not True:
            raise AssertionError("artifact.ok is True")

    async def test_unknown_target_raises_domain_error(self) -> None:
        """Профиль не в whitelist — доменное исключение с kind в EXPECTED."""
        from boba.tool.ch.tools import EXPECTED

        body = ToolMain.toolset(ch_query)[0].coroutine
        if body is None:
            raise AssertionError("body is not None")
        with pytest.raises(UnknownConnectionError) as caught:
            await body(sql="select 1", connection_name="нет-такого", cfg=ch_config())

        kind = ExpectedErrors.kind_of(caught.value, dict(EXPECTED))
        if kind != "unknown_target":
            raise AssertionError('kind == "unknown_target"')

    def test_empty_profiles_are_allowed(self) -> None:
        """Whitelist подставляет приложение на вызов: пустой — штатное состояние."""
        if ChToolConfig.model_validate({"profiles": {}}).targets():
            raise AssertionError("empty whitelist must list no targets")


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

    def test_client_settings_drop_none_and_reveal_password(self) -> None:
        config = ClickHouseConfig.model_validate(
            {**self._BASE, "username": "u", "password": "s3cret"}
        )
        settings = config.client_settings()
        if settings["password"] != "s3cret":
            raise AssertionError('settings["password"] == "s3cret"')
        if "ca_cert" in settings:
            raise AssertionError('"ca_cert" not in settings')
        if settings["settings"] != {}:
            raise AssertionError('settings["settings"] == {}')

    def test_password_is_masked_without_reveal_context(self) -> None:
        config = ClickHouseConfig.model_validate(
            {**self._BASE, "username": "u", "password": "s3cret"}
        )
        if config.model_dump(mode="json")["password"] is not None:
            raise AssertionError('config.model_dump(mode="json")["password"] is None')
        revealed = config.model_dump(
            mode="json", context={ClickHouseConfig.REVEAL_SECRETS: True}
        )
        if revealed["password"] != "s3cret":
            raise AssertionError('revealed["password"] == "s3cret"')

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
                "connect_timeout": 5,
            }
        )
        if config.service_name() != "HTTP@ch01.loshara.com":
            raise AssertionError('config.service_name() == "HTTP@ch01.loshara.com"')


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
