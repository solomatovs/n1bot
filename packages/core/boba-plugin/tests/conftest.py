"""Pytest-фикстуры пакета boba-plugin."""

from __future__ import annotations

import os

import pytest

from boba.plugin import ExtensionContext


@pytest.fixture(autouse=True)
def clean_boba_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Изолировать тесты от внешнего `BOBA_*`-окружения.

    Каждый install_plugins-тест начинался с серии `monkeypatch.delenv(...)` —
    autouse-фикстура снимает это с тестов один раз.
    """
    for key in list(os.environ):
        if key.startswith("BOBA_"):
            monkeypatch.delenv(key, raising=False)


@pytest.fixture
def empty_ctx() -> ExtensionContext:
    """`ExtensionContext()` без зарегистрированных расширений."""
    return ExtensionContext()
