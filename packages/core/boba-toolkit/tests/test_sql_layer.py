"""Общее у SQL-инструментов: whitelist профилей, приведение строк, результат."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any, ClassVar
from uuid import UUID

import pytest
from pydantic import BaseModel, ConfigDict, SecretStr, ValidationError

from boba.toolkit.launcher import RowStream
from boba.toolkit.result import AffectedSqlResult, ToolArtifact, render_for_llm
from boba.toolkit.sql import SqlProfiles, UnknownConnectionError


class FakeConn(BaseModel):
    """Профиль соединения выдуманного коннектора: минимум, но с секретом."""

    model_config = ConfigDict(extra="ignore")

    host: str
    password: SecretStr | None = None


class FakeProfiles(SqlProfiles[FakeConn]):
    SECTION: ClassVar[str] = "tool.fake"


def fake_profiles() -> FakeProfiles:
    return FakeProfiles.model_validate(
        {"profiles": {"main": {"host": "h", "password": "s3cret"}}}
    )


class TestSqlProfiles:
    def test_profile_keeps_connector_type(self) -> None:
        cfg = fake_profiles()
        assert cfg.targets() == ["main"]
        assert isinstance(cfg.resolve("main"), FakeConn)

    def test_unknown_connection_names_the_section(self) -> None:
        """Наружу идёт ошибка слоя, а не ValueError изнутри whitelist'а."""
        with pytest.raises(
            UnknownConnectionError, match=r"tool\.fake: connection_name"
        ):
            fake_profiles().resolve("нет-такого")

    def test_profiles_are_required(self) -> None:
        with pytest.raises(ValidationError, match="no profiles configured"):
            FakeProfiles.model_validate({"profiles": {}})

    def test_plain_dump_keeps_the_secret_masked(self) -> None:
        cfg = fake_profiles()
        dump = cfg.model_dump()
        assert dump["profiles"]["main"]["password"] != "s3cret"


class TestRowStreamPlain:
    def test_row_becomes_json_safe(self) -> None:
        row: dict[str, Any] = {
            "i": 1,
            "d": Decimal("1.5"),
            "u": UUID("00000000-0000-0000-0000-000000000001"),
            "dt": date(2026, 1, 2),
            "b": b"v",
            "arr": (1, 2),
            "map": {"k": b"v"},
            "empty": None,
        }
        assert RowStream.plain(row) == {
            "i": 1,
            "d": "1.5",
            "u": "00000000-0000-0000-0000-000000000001",
            "dt": "2026-01-02",
            "b": "v",
            "arr": [1, 2],
            "map": {"k": "v"},
            "empty": None,
        }

    def test_non_utf8_bytes_do_not_break_the_dump(self) -> None:
        plain = RowStream.plain({"raw": b"\xff\x00ok"})
        assert plain["raw"].endswith("ok")


class TestAffectedSqlResult:
    def test_status_wins_over_counter(self) -> None:
        result = AffectedSqlResult(affected_rows=5, status="DELETE 5")
        assert render_for_llm(result) == "DELETE 5"

    def test_counter_is_used_without_status(self) -> None:
        result = AffectedSqlResult(affected_rows=5, status=None)
        assert render_for_llm(result) == "affected rows: 5"

    def test_ddl_without_counter_still_reports_success(self) -> None:
        result = AffectedSqlResult(affected_rows=None, status=None)
        assert render_for_llm(result) == "statement executed"
        assert result.ok is True

    def test_artifact_survives_serialization(self) -> None:
        result = AffectedSqlResult(affected_rows=1, status="UPDATE 1")
        revived = ToolArtifact.revive(result.model_dump(mode="json"))
        assert revived == result
