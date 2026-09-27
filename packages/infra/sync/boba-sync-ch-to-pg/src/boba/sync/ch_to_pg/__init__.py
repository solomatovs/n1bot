"""Пара ClickHouse -> postgres: TabSeparated как COPY text, типы ClickHouse в типы
postgres."""

from boba.sync.ch_to_pg.transfer import ChPgTypes, ChToPg

__all__ = ["ChPgTypes", "ChToPg"]
