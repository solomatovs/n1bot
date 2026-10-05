"""Плагин pytest общего стенда: конфиг приложения, тестовая база и пул, kerberos.

Конфиг берётся как приложением: BOBA_CONFIG_PATH либо conf/config.toml в BOBA_BASE.
"""

from collections.abc import AsyncIterator, Iterator
from enum import StrEnum

import pytest
from omegaconf import DictConfig, OmegaConf

from boba.config import bind
from boba.db.postgres import AsyncPostgresPool
from boba.db.postgres.connection import PostgresConfig
from boba.runtime.config import ConfigLocator, RawConfig, RuntimeConfig
from boba.stand.database import TestDatabase
from boba.stand.refs import StandRefs
from boba.stand.site import StandLayers
from boba.stand.ui.stand import REPO_ROOT, StandPaths
from boba.stand.zygote import ZygoteStand
from boba.stand_core.context import CallStand, call_stand
from boba.toolkit.chain import CallAmbient

__all__ = ["call_stand"]


@pytest.fixture
def runtime_stand(call_stand: CallStand) -> Iterator[StandRefs]:
    """Объекты процесса для теста: реестр запусков и журналы вызовов поверх
    держателя контекста этого теста; способы запуска гасятся после теста."""
    stand = StandRefs(call_stand.contexts)
    try:
        yield stand
    finally:
        stand.stop()


@pytest.fixture
def call_ambient() -> CallAmbient:
    """Обстановка вызова теста: в неё тест ставит приёмники журнала, её же
    получают исполнители под тестом."""
    return CallAmbient()


@pytest.fixture(scope="session")
def zygote_stand() -> Iterator[ZygoteStand]:
    """Стенд зигот прогона: один реестр зигот на процесс тестов."""
    stand = ZygoteStand()
    try:
        yield stand
    finally:
        stand.stop()


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    return "asyncio"


class ServiceData(StrEnum):
    """Подкаталоги каталога данных сервиса, которые он ждёт готовыми."""

    WORKSPACE = "workspace"
    TOOL_LOGS = "tool-logs"
    DUMP = "dump"
    KRB = "krb"


@pytest.fixture(scope="session")
def raw_config() -> DictConfig:
    """Конфиг приложения со стендовым слоем conf/stand.toml поверх."""
    RawConfig.load(ConfigLocator.path())

    path = ConfigLocator.path()
    raw = StandLayers.compose(path)
    if not isinstance(raw, DictConfig):
        got = type(raw).__name__
        msg = f"stand config {path}: expected to compose into a table, got {got}"
        raise TypeError(msg)

    return raw


@pytest.fixture(scope="session")
def service_raw_config(tmp_path_factory: pytest.TempPathFactory) -> DictConfig:
    """Конфиг сервиса boba-mcp со стендовым слоем его conf/stand.toml: секции
    инструментов и их плагины живут у сервиса, который инструменты исполняет.

    Корень — дерево сервиса, каким бы приложением ни был запущен прогон:
    модели плагинов лежат там. Каталог данных — временный: образы workspace,
    журналы и выгрузки тестов не должны ложиться в данные развёрнутого
    приложения."""
    path = StandPaths.MCP_BASE_CONFIG.under(REPO_ROOT)
    raw = StandLayers.compose(path)
    if not isinstance(raw, DictConfig):
        got = type(raw).__name__
        msg = f"service config {path}: expected to compose into a table, got {got}"
        raise TypeError(msg)

    data = tmp_path_factory.mktemp("service-data")
    for name in ServiceData:
        (data / name.value).mkdir()

    OmegaConf.update(raw, "env.base", str(StandPaths.MCP_BASE.under(REPO_ROOT)))
    OmegaConf.update(raw, "env.data", str(data))

    return raw


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
def test_postgres(runtime_config: RuntimeConfig, test_database: str) -> PostgresConfig:
    """Профиль тестовой базы: тем, кто подключается сам, а не пулом."""
    return TestDatabase.config_of(runtime_config.data_layer.postgres, test_database)


@pytest.fixture
async def pool(
    runtime_config: RuntimeConfig, test_database: str
) -> AsyncIterator[AsyncPostgresPool]:
    """Пул в тестовой базе с search_path на схему хранения приложения."""
    postgres = TestDatabase.config_of(runtime_config.data_layer.postgres, test_database)
    p = AsyncPostgresPool(postgres.with_schema(runtime_config.data_layer.db_schema))
    await p.open()
    try:
        yield p
    finally:
        await p.close()
