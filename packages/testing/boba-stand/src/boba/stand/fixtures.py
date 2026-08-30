"""Плагин pytest общего стенда: конфиг приложения, тестовая база и пул, kerberos.

Конфиг берётся так же, как приложением: BOBA_CONFIG_PATH либо conf/config.toml в BOBA_BASE.
"""

from collections.abc import AsyncIterator

import pytest
from omegaconf import DictConfig

from boba.db.postgres import AsyncPostgresPool
from boba.runtime.config import ConfigLocator, RawConfig, RuntimeConfig
from boba.settings import bind
from boba.stand.context import call_context_cleared
from boba.stand.database import TestDatabase

__all__ = ["call_context_cleared"]


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(scope="session")
def raw_config() -> DictConfig:
    """Собранный конфиг приложения до привязки к моделям."""
    return RawConfig.load(ConfigLocator.path())


@pytest.fixture(scope="session")
def runtime_config(raw_config: DictConfig) -> RuntimeConfig:
    """Конфиг рантайма без побочных действий загрузчика: кэши kerberos ставит стенд."""
    return bind(raw_config, path=RuntimeConfig.SECTION, model=RuntimeConfig)


@pytest.fixture(scope="session", autouse=True)
def kerberos_workspace(
    runtime_config: RuntimeConfig, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """Кэши билетов теста: тела инструментов ждут настроенный workspace."""
    from boba.krb import KerberosWorkspace  # noqa: PLC0415

    cache = tmp_path_factory.mktemp("krb-cache")
    KerberosWorkspace.configure(runtime_config.krb.config, str(cache))


@pytest.fixture(scope="session")
async def test_database(runtime_config: RuntimeConfig) -> str:
    return await TestDatabase.ensure(runtime_config.data_layer.postgres)


@pytest.fixture
async def pool(
    runtime_config: RuntimeConfig, test_database: str
) -> AsyncIterator[AsyncPostgresPool]:
    """Пул в тестовой базе с search_path на схему хранения приложения."""
    p = AsyncPostgresPool(
        TestDatabase.config_of(runtime_config.data_layer.postgres, test_database),
        override_options={"search_path": runtime_config.data_layer.db_schema},
    )
    await p.open()
    try:
        yield p
    finally:
        await p.close()
