"""Пара ClickHouse -> ClickHouse: загрузка TabSeparated с родными типами ClickHouse."""

from boba.sync.ch_to_ch.transfer import ChToCh, ChTypeRules

__all__ = ["ChToCh", "ChTypeRules"]
