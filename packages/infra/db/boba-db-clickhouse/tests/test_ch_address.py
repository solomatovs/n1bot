"""Адреса ClickHouse: строка ↔ jsonb по таблице примеров, обязательный порт,
адрес базы по профилю соединения."""

from __future__ import annotations

import pytest
from pydantic import SecretStr

from boba.connections.address import AddressError
from boba.db.clickhouse.address import ChAddresses, ChNodeKind
from boba.db.clickhouse.connection import ClickHouseConfig, PasswordAuth

BASE = {"scheme": "clickhouse", "host": "ch1", "port": 9000, "database": "logs"}

EXAMPLES: list[tuple[ChNodeKind, dict[str, object], str]] = [
    (ChNodeKind.DATABASE, {}, "clickhouse://ch1:9000/logs"),
    (ChNodeKind.TABLE, {"table": "events"}, "clickhouse://ch1:9000/logs?table=events"),
    (
        ChNodeKind.COLUMN,
        {"table": "events", "column": "user_id"},
        "clickhouse://ch1:9000/logs?table=events&column=user_id",
    ),
    (
        ChNodeKind.VIEW,
        {"view": "v_events_hourly"},
        "clickhouse://ch1:9000/logs?view=v_events_hourly",
    ),
    (
        ChNodeKind.COLUMN,
        {"view": "v_events_hourly", "column": "hour"},
        "clickhouse://ch1:9000/logs?view=v_events_hourly&column=hour",
    ),
    (
        ChNodeKind.MATVIEW,
        {"matview": "mv_events_daily"},
        "clickhouse://ch1:9000/logs?matview=mv_events_daily",
    ),
    (
        ChNodeKind.COLUMN,
        {"matview": "mv_events_daily", "column": "day"},
        "clickhouse://ch1:9000/logs?matview=mv_events_daily&column=day",
    ),
    (
        ChNodeKind.INDEX,
        {"table": "events", "index": "events_ts_minmax"},
        "clickhouse://ch1:9000/logs?table=events&index=events_ts_minmax",
    ),
    (
        ChNodeKind.PROJECTION,
        {"table": "events", "projection": "events_by_user"},
        "clickhouse://ch1:9000/logs?table=events&projection=events_by_user",
    ),
    (
        ChNodeKind.DICTIONARY,
        {"dictionary": "dict_users"},
        "clickhouse://ch1:9000/logs?dictionary=dict_users",
    ),
    (
        ChNodeKind.FUNCTION,
        {"function": "to_rub"},
        "clickhouse://ch1:9000/logs?function=to_rub",
    ),
]


@pytest.mark.parametrize(("kind", "roles", "text"), EXAMPLES)
def test_examples_round_trip(
    kind: ChNodeKind, roles: dict[str, object], text: str
) -> None:
    address = ChAddresses.parse(kind, text)

    assert kind == type(address).KIND
    assert address.to_json() == {**BASE, **roles}
    assert address.render() == text

    assert ChAddresses.parse_any(text) == address


def test_port_is_required() -> None:
    with pytest.raises(AddressError, match="port is required"):
        ChAddresses.parse(ChNodeKind.DATABASE, "clickhouse://ch1/logs")


def test_kind_must_match_roles() -> None:
    with pytest.raises(AddressError, match="matches none of its shapes"):
        ChAddresses.parse(ChNodeKind.TABLE, "clickhouse://ch1:9000/logs?view=v")


def test_postgres_scheme_is_refused() -> None:
    with pytest.raises(AddressError, match="expected scheme clickhouse"):
        ChAddresses.parse(ChNodeKind.DATABASE, "postgresql://ch1:9000/logs")


def _profile(**parts: object) -> ClickHouseConfig:
    return ClickHouseConfig.model_validate(
        {
            "host": "ch1",
            "port": 8123,
            "interface": "http",
            "auth": PasswordAuth(
                method="password", user="app", password=SecretStr("x")
            ),
            **parts,
        }
    )


def test_base_of_profile() -> None:
    address = ChAddresses.base_of(_profile(database="logs"))

    assert address.render() == "clickhouse://ch1:8123/logs"


def test_base_of_profile_without_database_is_refused() -> None:
    with pytest.raises(AddressError, match="no default database"):
        ChAddresses.base_of(_profile())
