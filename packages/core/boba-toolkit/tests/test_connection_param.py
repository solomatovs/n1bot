"""Параметр-соединение: манифест называет его и вид, профиль едет каналом
аргументов, argv остаётся чистым."""

from __future__ import annotations

from typing import Annotated, Literal

import pytest
from pydantic import Field, SecretStr, SerializationInfo, field_serializer

from boba.core.secrets import SecretRevealing
from boba.toolkit.entry import ToolArgv, ToolEntryError
from boba.toolkit.facade import Injected, PayloadTool, UserConnection, tool
from boba.toolkit.manifest import ConnectionProfile, InjectedRole, ManifestBuilder
from boba.toolkit.protocol import ToolCommand
from boba.toolkit.result import MarkdownResult

PASSWORD = "connection-secret-value"
TOKEN = "section-secret-value"


class StoredProfile(ConnectionProfile):
    """Профиль пакета-владельца: своей базы раскрытия у него нет."""

    kind: Literal["stored"] = "stored"
    host: str
    password: SecretStr


class GuardedProfile(ConnectionProfile):
    """Профиль, чей секрет наружу не уезжает никогда: политика поля."""

    kind: Literal["guarded"] = "guarded"
    host: str
    password: SecretStr

    @field_serializer("password", when_used="json")
    def _dump_password(self, value: SecretStr, info: SerializationInfo) -> str:
        return str(value)


class SectionConfig(SecretRevealing):
    """Обычный injected-конфиг секции рядом с параметром-соединением."""

    token: SecretStr
    max_rows: int = 10


@tool
async def probe_query(
    connection: Annotated[StoredProfile, UserConnection],
    sql: Annotated[str, Field(min_length=1, description="Запрос")],
    cfg: Annotated[SectionConfig, Injected],
) -> MarkdownResult:
    """Выполняет запрос на соединении пользователя."""
    return MarkdownResult(text=f"{connection.host}|{sql}|{cfg.max_rows}")


@tool
async def probe_copy(
    source: Annotated[StoredProfile, UserConnection],
    target: Annotated[StoredProfile, UserConnection],
) -> MarkdownResult:
    """Перекачивает данные между двумя соединениями пользователя."""
    return MarkdownResult(text=f"{source.host}->{target.host}")


@tool
async def probe_guarded(
    connection: Annotated[GuardedProfile, UserConnection],
) -> MarkdownResult:
    """Работает с профилем, чей секрет остаётся в приложении."""
    return MarkdownResult(text=connection.host)


def _profile(host: str = "db.local") -> StoredProfile:
    return StoredProfile(host=host, password=SecretStr(PASSWORD))


def _section() -> SectionConfig:
    return SectionConfig(token=SecretStr(TOKEN))


BUILDER = ManifestBuilder()


def _command(payload: PayloadTool, kwargs: dict[str, object]) -> ToolCommand:
    return ToolArgv.render(BUILDER.tool(payload), kwargs, {}, {})


class TestManifestSplitsParameters:
    def test_connection_is_not_an_injected_parameter(self) -> None:
        decl = BUILDER.tool(probe_query)

        assert decl.connections == {"connection": "stored"}
        assert decl.injected == {"cfg": InjectedRole.CONFIG}
        assert list(decl.properties()) == ["sql"]

    def test_every_marked_parameter_is_collected(self) -> None:
        decl = BUILDER.tool(probe_copy)

        assert decl.connections == {"source": "stored", "target": "stored"}


class TestProfileTravelsOffArgv:
    def test_argv_carries_only_the_tool_name(self) -> None:
        command = _command(
            probe_query,
            {"connection": _profile(), "sql": "select 1", "cfg": _section()},
        )

        assert command.argv == ("probe_query",)

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

        assert PASSWORD.encode() in command.args
        assert TOKEN.encode() in command.args


class TestBodyGetsModels:
    def test_parse_restores_profile_and_section(self) -> None:
        command = _command(
            probe_query,
            {"connection": _profile(), "sql": "select 1", "cfg": _section()},
        )

        kwargs = ToolArgv.parse(probe_query, (), command.args)

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

        kwargs = ToolArgv.parse(probe_copy, (), command.args)

        assert kwargs["source"].host == "left"
        assert kwargs["target"].host == "right"

    def test_missing_profile_is_an_entry_error(self) -> None:
        with pytest.raises(ToolEntryError, match="connection"):
            ToolArgv.parse(probe_query, (), b'{"sql": "x", "cfg": {"token": "x"}}')


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

        assert PASSWORD.encode() not in command.args
