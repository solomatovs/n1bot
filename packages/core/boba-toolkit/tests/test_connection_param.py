"""Параметр-соединение: профиль едет каналом конфига, argv остаётся чистым."""

from __future__ import annotations

from typing import Annotated, ClassVar, Literal

import pytest
from pydantic import BaseModel, Field, SecretStr, SerializationInfo, field_serializer

from boba.toolkit.entry import ToolAddress, ToolArgv, ToolEntryError
from boba.toolkit.facade import Injected, PayloadTool, UserConnection, tool
from boba.toolkit.protocol import ToolCommand
from boba.toolkit.result import TextResult, ToolResult, pack_result
from boba.toolkit.types import SecretRevealing

PASSWORD = "connection-secret-value"
TOKEN = "section-secret-value"


class StoredProfile(BaseModel):
    """Профиль пакета-владельца: своей базы раскрытия у него нет."""

    kind: Literal["stored"] = "stored"
    host: str
    password: SecretStr


class GuardedProfile(BaseModel):
    """Профиль, чей секрет наружу не уезжает никогда: политика поля."""

    kind: Literal["guarded"] = "guarded"
    host: str
    password: SecretStr

    @field_serializer("password", when_used="json")
    def _dump_password(self, value: SecretStr, info: SerializationInfo) -> str:
        return str(value)


class SectionConfig(SecretRevealing):
    """Обычный injected-конфиг секции рядом с параметром-соединением."""

    SECTION: ClassVar[str] = "tool.probe"

    token: SecretStr
    max_rows: int = 10


@tool
async def probe_query(
    connection: Annotated[StoredProfile, UserConnection],
    sql: Annotated[str, Field(min_length=1, description="Запрос")],
    cfg: Annotated[SectionConfig, Injected],
) -> tuple[str, ToolResult]:
    """Выполняет запрос на соединении пользователя."""
    return pack_result(TextResult(text=f"{connection.host}|{sql}|{cfg.max_rows}"))


@tool
async def probe_copy(
    source: Annotated[StoredProfile, UserConnection],
    target: Annotated[StoredProfile, UserConnection],
) -> tuple[str, ToolResult]:
    """Перекачивает данные между двумя соединениями пользователя."""
    return pack_result(TextResult(text=f"{source.host}->{target.host}"))


@tool
async def probe_guarded(
    connection: Annotated[GuardedProfile, UserConnection],
) -> tuple[str, ToolResult]:
    """Работает с профилем, чей секрет остаётся в приложении."""
    return pack_result(TextResult(text=connection.host))


def _profile(host: str = "db.local") -> StoredProfile:
    return StoredProfile(host=host, password=SecretStr(PASSWORD))


def _section() -> SectionConfig:
    return SectionConfig(token=SecretStr(TOKEN))


def _command(payload: PayloadTool, kwargs: dict[str, object]) -> ToolCommand:
    address = ToolAddress.of(payload)

    return ToolArgv.render(address, payload.args_schema, kwargs)


def _tail(command: ToolCommand) -> list[str]:
    """argv без головы `python3 -m <модуль> <имя>`."""
    return list(command.argv[4:])


class TestSchemaSplitsParameters:
    def test_connection_is_not_an_injected_field(self) -> None:
        schema = probe_query.args_schema

        assert list(ToolArgv.connection_fields(schema)) == ["connection"]
        assert list(ToolArgv.injected_fields(schema)) == ["cfg"]

    def test_every_marked_parameter_is_collected(self) -> None:
        fields = ToolArgv.connection_fields(probe_copy.args_schema)

        assert list(fields) == ["source", "target"]


class TestProfileTravelsOffArgv:
    def test_argv_carries_only_llm_arguments(self) -> None:
        command = _command(
            probe_query,
            {"connection": _profile(), "sql": "select 1", "cfg": _section()},
        )

        assert _tail(command) == ["--sql", "select 1"]

    def test_secrets_never_reach_argv(self) -> None:
        command = _command(
            probe_query,
            {"connection": _profile(), "sql": "select 1", "cfg": _section()},
        )

        joined = " ".join(command.argv)
        assert PASSWORD not in joined
        assert TOKEN not in joined

    def test_config_carries_profile_with_open_secret(self) -> None:
        command = _command(
            probe_query,
            {"connection": _profile(), "sql": "select 1", "cfg": _section()},
        )

        assert PASSWORD.encode() in command.config
        assert TOKEN.encode() in command.config


class TestBodyGetsModels:
    def test_parse_restores_profile_and_section(self) -> None:
        command = _command(
            probe_query,
            {"connection": _profile(), "sql": "select 1", "cfg": _section()},
        )

        kwargs = ToolArgv.parse(probe_query, _tail(command), command.config)

        connection = kwargs["connection"]
        assert isinstance(connection, StoredProfile)
        assert connection.password.get_secret_value() == PASSWORD
        assert kwargs["cfg"].token.get_secret_value() == TOKEN
        assert kwargs["sql"] == "select 1"

    def test_two_connections_stay_separate(self) -> None:
        command = _command(
            probe_copy,
            {"source": _profile("left"), "target": _profile("right")},
        )

        kwargs = ToolArgv.parse(probe_copy, _tail(command), command.config)

        assert kwargs["source"].host == "left"
        assert kwargs["target"].host == "right"

    def test_missing_profile_is_an_entry_error(self) -> None:
        command = _command(
            probe_query,
            {"connection": _profile(), "sql": "select 1", "cfg": _section()},
        )

        with pytest.raises(ToolEntryError, match="connection"):
            ToolArgv.parse(probe_query, _tail(command), b'{"cfg": {"token": "x"}}')


class TestFieldPolicyWins:
    def test_guarded_secret_stays_masked(self) -> None:
        command = _command(
            probe_guarded,
            {
                "connection": GuardedProfile(
                    host="db.local", password=SecretStr(PASSWORD)
                )
            },
        )

        assert PASSWORD.encode() not in command.config
