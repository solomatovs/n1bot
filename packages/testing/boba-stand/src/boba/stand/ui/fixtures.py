"""Плагин pytest UI-стенда: один sync-playwright и один браузер на сессию.

Sync API держит запущенный asyncio-loop в главном потоке, пока жив: второй
sync_playwright() в этом потоке падает, поэтому экземпляр общий для всех пакетов.
"""

from collections.abc import Iterator

import pytest


@pytest.fixture(scope="session")
def playwright() -> Iterator[object]:
    from playwright.sync_api import sync_playwright  # noqa: PLC0415

    with sync_playwright() as instance:
        yield instance


@pytest.fixture(scope="session")
def browser(playwright: object) -> Iterator[object]:
    from playwright.sync_api import Playwright  # noqa: PLC0415

    if not isinstance(playwright, Playwright):
        got = type(playwright).__name__
        msg = f"playwright fixture: expected a sync Playwright instance, got {got}"
        raise TypeError(msg)

    instance = playwright.chromium.launch(args=["--no-sandbox"])
    try:
        yield instance
    finally:
        instance.close()
