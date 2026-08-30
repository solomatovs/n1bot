"""Плагин pytest UI-стенда: один sync-playwright и один браузер на сессию.

Sync API держит запущенный asyncio-loop в главном потоке, пока жив: второй
sync_playwright() в этом потоке падает, поэтому экземпляр общий для всех пакетов.
"""

from collections.abc import Iterator

import pytest


@pytest.fixture(scope="session")
def playwright() -> Iterator[object]:
    sync_api = pytest.importorskip("playwright.sync_api", reason="ui-тестам нужен playwright")
    with sync_api.sync_playwright() as instance:
        yield instance


@pytest.fixture(scope="session")
def browser(playwright: object) -> Iterator[object]:
    from playwright.sync_api import Playwright  # noqa: PLC0415

    if not isinstance(playwright, Playwright):
        raise TypeError("playwright fixture must be a sync Playwright instance")

    instance = playwright.chromium.launch(args=["--no-sandbox"])
    try:
        yield instance
    finally:
        instance.close()
