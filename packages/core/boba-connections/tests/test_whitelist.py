"""Выбор соединения под вызов по имени: дубли имён — неоднозначность."""

from __future__ import annotations

from uuid import UUID

import pytest

from boba.connections.profile import GrantedConnection, StoredConnection
from boba.connections.whitelist import (
    AmbiguousConnectionError,
    ConnectionWhitelist,
)
from boba.transport.http.profile import HttpConnection


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    """Чистая логика: сессия приложения не нужна."""


def _row(
    row_id: UUID, name: str, host: str, *, ambiguous: bool = False
) -> GrantedConnection:
    stored = StoredConnection(
        id=row_id,
        name=name,
        profile=HttpConnection(host=host, port=443, ssl_verify=False),
    )
    return GrantedConnection(row=stored, ambiguous=ambiguous)


def _whitelist(*rows: GrantedConnection) -> ConnectionWhitelist:
    return ConnectionWhitelist.of(rows)


class TestPick:
    def test_name_is_matched_exactly(self) -> None:
        whitelist = _whitelist(_row(UUID(int=1), "confl", "wiki.example.com"))
        picked = whitelist.pick("confl")
        if picked is None or picked.name != "confl":
            raise AssertionError(f"name must match exactly: {picked}")
        if whitelist.pick("conf") is not None or whitelist.pick("*") is not None:
            raise AssertionError("names have no patterns")

    def test_duplicate_name_is_ambiguous(self) -> None:
        whitelist = _whitelist(
            _row(UUID(int=1), "confl", "wiki.example.com", ambiguous=True),
            _row(UUID(int=2), "confl", "*.example.com", ambiguous=True),
        )
        if "confl" not in whitelist.ambiguous or whitelist.profiles:
            raise AssertionError("duplicate name must be ambiguous and unlisted")
        with pytest.raises(AmbiguousConnectionError):
            whitelist.pick("confl")

    def test_names_list_only_unambiguous_rows(self) -> None:
        whitelist = _whitelist(
            _row(UUID(int=1), "confl", "wiki.example.com"),
            _row(UUID(int=2), "dup", "a.example.com", ambiguous=True),
            _row(UUID(int=3), "dup", "b.example.com", ambiguous=True),
        )
        if whitelist.names() != ("confl",):
            raise AssertionError(f"ambiguous names stay unlisted: {whitelist.names()}")
