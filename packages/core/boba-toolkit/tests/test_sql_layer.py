"""Общий SQL-слой: параметризация generic'ами, секреты, лимиты, нормализация."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from decimal import Decimal
from typing import Any, ClassVar
from uuid import UUID

import pytest
from pydantic import (
    ConfigDict,
    SecretStr,
    SerializationInfo,
    ValidationError,
    field_serializer,
)

from boba.toolkit.launcher import CollectorCapacityError, CollectorRowLimitError
from boba.toolkit.result import AffectedSqlResult, ToolArtifact, render_for_llm
from boba.toolkit.sql import (
    ConnectionProfile,
    SqlCall,
    SqlErrors,
    SqlProfiles,
    SqlQueryError,
    SqlQueryRequest,
    SqlQueryTrailer,
    SqlRows,
    UnknownConnectionError,
)


class FakeConn(ConnectionProfile):
    """Профиль соединения выдуманного коннектора: минимум, но с секретом."""

    model_config = ConfigDict(extra="ignore")

    REVEAL_SECRETS: ClassVar[str] = "reveal_secrets"

    host: str
    password: SecretStr | None = None

    @field_serializer("password", when_used="json")
    def _dump_password(
        self, value: SecretStr | None, info: SerializationInfo
    ) -> str | None:
        if value is None:
            return None
        context = info.context
        if not isinstance(context, Mapping):
            return None
        if not context.get(FakeConn.REVEAL_SECRETS):
            return None
        return value.get_secret_value()


class FakeProfiles(SqlProfiles[FakeConn]):
    SECTION: ClassVar[str] = "tool.fake"


class FakeQueryRequest(SqlQueryRequest[FakeConn, tuple[Any, ...]]):
    OP: ClassVar[str] = "fake_query"


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


class TestSqlQueryRequest:
    def test_secrets_are_revealed_only_in_payload_dump(self) -> None:
        """stdin песочницы — доверенный канал; в обычном дампе пароля нет."""
        request = FakeQueryRequest(
            op=FakeQueryRequest.OP,
            connection=fake_profiles().resolve("main"),
            sql="select 1",
            params=(1, "a"),
            row_limit=10,
        )
        assert '"password":"s3cret"' in request.model_dump_json()
        assert request.model_dump()["connection"]["password"] != "s3cret"

    def test_params_stay_out_of_the_base_call(self) -> None:
        """SqlCall несёт только соединение и SQL: параметры добавляет наследник."""
        assert "params" not in SqlCall.model_fields
        assert set(FakeQueryRequest.model_fields) == {
            "op",
            "connection",
            "sql",
            "params",
            "row_limit",
        }


class TestSqlQueryTrailer:
    def test_all_fields_are_required(self) -> None:
        with pytest.raises(ValidationError):
            SqlQueryTrailer.model_validate({"truncated": False})

    def test_dml_trailer_carries_rowcount(self) -> None:
        trailer = SqlQueryTrailer.model_validate(
            {
                "truncated": False,
                "returns_rows": False,
                "rowcount": 5,
                "status": "DELETE 5",
            }
        )
        assert trailer.returns_rows is False
        assert trailer.rowcount == 5


class TestSqlErrors:
    def test_limits_are_named_in_the_message(self) -> None:
        errors = SqlErrors(max_rows=100, max_bytes=1000)
        assert "1000" in errors.too_large().message
        assert "100" in errors.too_many_rows().message
        assert errors.too_large().ok is False

    def test_note_appears_only_on_truncation(self) -> None:
        errors = SqlErrors(max_rows=100, max_bytes=1000)
        assert errors.note(truncated=False) is None
        assert "100" in str(errors.note(truncated=True))

    def test_kinds_are_stable(self) -> None:
        errors = SqlErrors(max_rows=1, max_bytes=1)
        assert errors.failed(SqlQueryError("boom")).error_kind == "sql_failed"
        unknown = UnknownConnectionError("no")
        assert errors.unknown_target(unknown).error_kind == "unknown_target"

    def test_pack_maps_each_error_to_its_kind(self) -> None:
        errors = SqlErrors(max_rows=100, max_bytes=1000)
        assert errors.pack(UnknownConnectionError("no")).error_kind == "unknown_target"
        assert errors.pack(SqlQueryError("boom")).error_kind == "sql_failed"
        too_large = errors.pack(CollectorCapacityError("big"))
        assert too_large.error_kind == "result_too_large"
        assert errors.pack(CollectorRowLimitError()).error_kind == "too_many_rows"

    def test_catches_work_as_except_clause(self) -> None:
        """Фасад ловит одной веткой всё, что pack умеет упаковать."""
        errors = SqlErrors(max_rows=1, max_bytes=1)
        try:
            raise CollectorCapacityError("big")
        except SqlErrors.CATCHES as e:
            packed = errors.pack(e)
        assert packed.error_kind == "result_too_large"


class TestSqlRows:
    def test_mapping_row_becomes_json_safe(self) -> None:
        row = {
            "i": 1,
            "d": Decimal("1.5"),
            "u": UUID("00000000-0000-0000-0000-000000000001"),
            "dt": date(2026, 1, 2),
            "b": b"v",
            "empty": None,
        }
        assert SqlRows.of_mapping(row) == {
            "i": 1,
            "d": "1.5",
            "u": "00000000-0000-0000-0000-000000000001",
            "dt": "2026-01-02",
            "b": "v",
            "empty": None,
        }

    def test_column_row_becomes_json_safe(self) -> None:
        names = ("arr", "map", "st")
        row = ((1, 2), {"k": b"v"}, {3})
        assert SqlRows.of_columns(names, row) == {
            "arr": [1, 2],
            "map": {"k": "v"},
            "st": [3],
        }

    def test_set_order_is_deterministic(self) -> None:
        assert SqlRows.scalar({"b", "c", "a"}) == ["a", "b", "c"]
        assert SqlRows.scalar(frozenset({3, 1, 2})) == [1, 2, 3]


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
