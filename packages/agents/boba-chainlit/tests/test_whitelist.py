"""Выбор соединения под вызов по имени: дубли имён — неоднозначность."""

from __future__ import annotations

import pytest

from boba.connections.http import HttpProfile
from boba.connections.profile import StoredConnection
from boba.connections.whitelist import (
    AmbiguousConnectionError,
    ConnectionKeying,
    ConnectionWhitelist,
)


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    """Чистая логика: сессия приложения не нужна."""


def _row(row_id: int, name: str, base_url: str) -> StoredConnection:
    return StoredConnection(
        id=row_id, name=name, profile=HttpProfile(base_url=base_url, ssl_verify=False)
    )


def _whitelist(*rows: StoredConnection) -> ConnectionWhitelist:
    return ConnectionWhitelist.of(rows, ConnectionKeying.NAME)


class TestPick:
    def test_name_is_matched_exactly(self) -> None:
        whitelist = _whitelist(_row(1, "confl", "https://wiki.example.com"))
        picked = whitelist.pick("confl")
        if picked is None or picked.key != "confl":
            raise AssertionError(f"name must match exactly: {picked}")
        if whitelist.pick("conf") is not None or whitelist.pick("*") is not None:
            raise AssertionError("names have no patterns")

    def test_duplicate_name_is_ambiguous(self) -> None:
        whitelist = _whitelist(
            _row(1, "confl", "https://wiki.example.com"),
            _row(2, "confl", "https://*.example.com"),
        )
        if "confl" not in whitelist.ambiguous or whitelist.profiles:
            raise AssertionError("duplicate name must be ambiguous and unlisted")
        with pytest.raises(AmbiguousConnectionError):
            whitelist.pick("confl")

    def test_requested_from_kwargs(self) -> None:
        keying = ConnectionKeying.NAME
        if keying.requested({"connection_name": "x", "url": "https://h"}) != "x":
            raise AssertionError("connection_name must be the key")
        if keying.requested({"url": "https://h"}) != "":
            raise AssertionError("missing connection_name must be empty")
