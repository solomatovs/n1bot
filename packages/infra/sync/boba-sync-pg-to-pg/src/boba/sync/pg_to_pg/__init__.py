"""Пара postgres -> postgres: загрузка по COPY с родными типами postgres."""

from boba.sync.pg_to_pg.transfer import PgToPg

__all__ = ["PgToPg"]
