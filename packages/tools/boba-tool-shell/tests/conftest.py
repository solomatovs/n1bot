"""Фикстуры пакета: изоляция от chainlit-контекста общего conftest'а."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    pass
