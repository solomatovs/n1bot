"""Общие фикстуры прогонов: конфиг приложения, стенд и anyio-бэкенд."""

from __future__ import annotations

from pathlib import Path

import pytest
from omegaconf import DictConfig
from stand_site import Stand

from boba.chainlit.infra.entry import AppEntry
from boba.settings import build_app_config


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(scope="session", autouse=True)
def kerberos_workspace(tmp_path_factory: pytest.TempPathFactory) -> None:
    """Кэши билетов теста: тела инструментов ждут настроенный workspace."""
    from boba.krb import KerberosWorkspace  # noqa: PLC0415

    krb = Path(__file__).resolve().parents[1] / "compose" / "conf" / "krb"
    cache = tmp_path_factory.mktemp("krb-cache")
    KerberosWorkspace.configure(str(krb / "krb5.conf"), str(cache))


@pytest.fixture(scope="session")
def raw_config() -> DictConfig:
    """Конфиг приложения: BOBA_CONFIG_PATH либо conf/config.toml в BOBA_BASE."""
    return build_app_config(config_path=AppEntry.config_path())


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
