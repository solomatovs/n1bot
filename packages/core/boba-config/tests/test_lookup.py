"""ConfigLookup: Found / NotFound семантика."""

from __future__ import annotations

import pytest

from boba.config.path import Found, NotFound


def test_found_yields_value():
    f: Found[int] = Found(42)
    assert f.is_found()
    assert f.value() == 42
    assert f.or_else(0) == 42


def test_notfound_raises_on_value():
    nf: NotFound[int] = NotFound()
    assert not nf.is_found()
    with pytest.raises(LookupError):
        nf.value()
    assert nf.or_else(7) == 7


def test_found_value_can_be_none():
    f: Found[int | None] = Found(None)
    assert f.is_found()
    assert f.value() is None
