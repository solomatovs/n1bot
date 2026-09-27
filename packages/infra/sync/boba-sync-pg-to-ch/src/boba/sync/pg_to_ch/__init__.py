"""Пара postgres -> ClickHouse: COPY text как TabSeparated, типы postgres в типы
ClickHouse."""

from boba.sync.pg_to_ch.transfer import PgChTypes, PgToCh

__all__ = ["PgChTypes", "PgToCh"]
