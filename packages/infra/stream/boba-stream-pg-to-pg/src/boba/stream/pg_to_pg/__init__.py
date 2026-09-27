"""Пара postgres -> postgres: загрузка по COPY с родными типами postgres."""

from boba.stream.pg_to_pg.transfer import PgToPg

__all__ = ["PgToPg"]
