"""boba.pump_stand — стенд перекачки между базами: источники всех баз из
[ix_stand], порты в памяти и тела насосов postgres, ClickHouse и Oracle.
Тесты пакета гоняют цепочки между плагинами, чтобы сами плагины друг о
друге не знали."""

from __future__ import annotations

from boba.pump_stand.oracle import OracleStand
from boba.pump_stand.ports import Feed, Pipe, Sink
from boba.pump_stand.pumps import Chained, Leg, Pumps
from boba.pump_stand.sides import ClickHouseSide, OracleSide, PostgresSide
from boba.pump_stand.stand import ChSource, OraSource, PgSource, PumpStand

__all__ = [
    "ChSource",
    "Chained",
    "ClickHouseSide",
    "Feed",
    "Leg",
    "OraSource",
    "OracleSide",
    "OracleStand",
    "PgSource",
    "Pipe",
    "PostgresSide",
    "PumpStand",
    "Pumps",
    "Sink",
]
