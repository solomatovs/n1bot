"""Фикстуры стенда инструментов Oracle; модели и помощники лежат в ora_tool_stand."""

from __future__ import annotations

import pytest
from ora_tool_stand import IxStand


@pytest.fixture(scope="session")
def ix_stand() -> IxStand:
    return IxStand.required()
