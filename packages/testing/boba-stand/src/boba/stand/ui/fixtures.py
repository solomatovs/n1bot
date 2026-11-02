"""Плагин pytest UI-стенда: один sync-playwright и браузер на модуль.

Sync API держит asyncio-loop главного потока. Закрытие на границе модуля
освобождает поток для асинхронных тестов других компонентов.
"""

from collections.abc import Iterator

import pytest


@pytest.fixture(scope="module")
def playwright() -> Iterator[object]:
    from playwright.sync_api import sync_playwright  # noqa: PLC0415

    with sync_playwright() as instance:
        yield instance


@pytest.fixture(scope="module")
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
