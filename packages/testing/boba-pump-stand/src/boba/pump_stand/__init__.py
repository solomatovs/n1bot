"""boba.pump_stand — стенд перекачки между базами: источники всех баз из
[ix_stand], порты в памяти и тела насосов postgres, ClickHouse и Oracle.
Тесты пакета гоняют цепочки между плагинами, чтобы сами плагины друг о
друге не знали."""

from __future__ import annotations

from boba.pump_stand.oracle import OracleStand
from boba.pump_stand.ports import Feed, Sink
from boba.pump_stand.pumps import Pumps
from boba.pump_stand.stand import ChSource, OraSource, PgSource, PumpStand

__all__ = [
    "ChSource",
    "Feed",
    "OraSource",
    "OracleStand",
    "PgSource",
    "PumpStand",
    "Pumps",
    "Sink",
]
