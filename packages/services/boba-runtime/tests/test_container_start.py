"""Старт контейнера: сорвавшийся eager-провайдер закрывает уже поднятое."""

from __future__ import annotations

from collections.abc import AsyncGenerator, Generator

import pytest

from boba.runtime.di import Container

pytestmark = pytest.mark.anyio


class StartFailedError(Exception):
    """Отказ провайдера на старте."""


class Resources:
    """Провайдеры контейнера с учётом поднятого и закрытого."""

    def __init__(self) -> None:
        self.opened: list[str] = []
        self.closed: list[str] = []

    def launchers(self) -> Generator[str, None, None]:
        self.opened.append("launchers")
        try:
            yield "launchers"
        finally:
            self.closed.append("launchers")

    async def bus(self) -> AsyncGenerator[str, None]:
        self.opened.append("bus")
        try:
            yield "bus"
        finally:
            self.closed.append("bus")

    async def store(self) -> str:
        raise StartFailedError("store: the database gave no connection")

    async def late(self) -> AsyncGenerator[str, None]:
        self.opened.append("late")
        yield "late"


async def test_failed_start_closes_what_was_started() -> None:
    resources = Resources()
    container = Container(level="app")
    container.eager(resources.launchers, resources.bus, resources.store, resources.late)

    with pytest.raises(StartFailedError):
        await container.start()

    assert resources.opened == ["launchers", "bus"]
    assert resources.closed == ["bus", "launchers"]


async def test_started_container_closes_on_aclose_only() -> None:
    resources = Resources()
    container = Container(level="app")
    container.eager(resources.launchers, resources.bus)

    await container.start()

    assert resources.closed == []

    await container.aclose()

    assert resources.closed == ["bus", "launchers"]
