"""Образец снимка ClickHouse для тестов, стендов и показа: база dwh с
таблицей MergeTree, представлениями и словарём."""

from __future__ import annotations

from boba.db.clickhouse.snapshot import (
    ChColumn,
    ChDatabase,
    ChDictionary,
    ChDictionaryAttribute,
    ChSnapshot,
    ChTable,
    ChTableKind,
)

__all__ = ["ChSample"]


class ChSample:
    """База dwh: таблица MergeTree, представление, материализованное, словарь."""

    def __init__(self) -> None:
        self.database = ChDatabase(name="dwh", engine="Atomic")
        self.events = ChTable(
            database="dwh",
            name="events",
            kind=ChTableKind.TABLE,
            engine="MergeTree",
            engine_full="MergeTree ORDER BY (ts, user_id)",
            partition_key="toYYYYMM(ts)",
            sorting_key="ts, user_id",
            primary_key="ts, user_id",
            total_rows=10_000_000,
            total_bytes=2_000_000_000,
            create_query="CREATE TABLE dwh.events (...) ENGINE = MergeTree",
        )
        self.events_daily = ChTable(
            database="dwh",
            name="events_daily",
            kind=ChTableKind.MATERIALIZED,
            engine="MaterializedView",
            definition=(
                "SELECT toDate(ts) AS day, count() AS n FROM dwh.events GROUP BY day"
            ),
            target="dwh.events_daily_data",
        )
        self.v_events = ChTable(
            database="dwh",
            name="v_events",
            kind=ChTableKind.VIEW,
            engine="View",
            definition="SELECT * FROM dwh.events WHERE user_id > 0",
        )
        self.events_ts = ChColumn(
            database="dwh",
            table="events",
            name="ts",
            position=1,
            type="DateTime64(3)",
            in_sorting_key=True,
            in_primary_key=True,
            in_partition_key=True,
        )
        self.events_user = ChColumn(
            database="dwh",
            table="events",
            name="user_id",
            position=2,
            type="UInt64",
            in_sorting_key=True,
            in_primary_key=True,
        )
        self.events_payload = ChColumn(
            database="dwh",
            table="events",
            name="payload",
            position=3,
            type="String",
            codec="ZSTD(3)",
            comment="JSON события",
        )
        self.users_dict = ChDictionary(
            database="dwh",
            name="users",
            status="LOADED",
            layout="Hashed",
            source="ClickHouse(dwh.users_src)",
            key_columns=("user_id",),
            lifetime_min=300,
            lifetime_max=600,
        )
        self.users_name = ChDictionaryAttribute(
            database="dwh", dictionary="users", name="name", position=1, type="String"
        )

    def snapshot(self) -> ChSnapshot:
        return ChSnapshot(
            databases=(self.database,),
            tables=(self.events, self.events_daily, self.v_events),
            columns=(self.events_ts, self.events_user, self.events_payload),
            dictionaries=(self.users_dict,),
            dictionary_attributes=(self.users_name,),
        )
