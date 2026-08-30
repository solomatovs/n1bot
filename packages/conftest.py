"""Общие фикстуры прогонов: конфиг приложения, стенд и anyio-бэкенд."""

from __future__ import annotations

import pytest

from boba.stand.site import Stand

pytest_plugins = ["boba.stand.fixtures", "boba.stand.ui.fixtures"]


@pytest.fixture(scope="session")
def stand() -> Stand:
    """Адреса, принципалы и учётки стенда: в коде тестов их быть не должно."""
    return Stand.load()


@pytest.fixture(scope="session")
def live_kdc(stand: Stand) -> None:
    """Пропуск теста, когда локального AD на машине нет."""
    if stand.live():
        return

    pytest.skip("нет keytab/krb5.conf локального AD")
