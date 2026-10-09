"""Плагин pytest общего стенда: конфиг приложения, тестовая база и пул, kerberos.

Файлы конфигурации называет окружение: BOBA_CONFIG_PATH и BOBA_SITE_PATH.
"""

from collections.abc import AsyncIterator, Iterator
from copy import deepcopy
from enum import StrEnum

import pytest
from omegaconf import DictConfig, OmegaConf

from boba.config import bind
from boba.db.postgres import AsyncPostgresPool
from boba.db.postgres.connection import PostgresConfig
from boba.runtime.config import (
    ConfigLocator,
    DataLayerConfig,
    EnvOverride,
    ProcessConfig,
    RawConfig,
    RuntimeConfig,
)
from boba.stand.database import TestDatabase
from boba.stand.refs import StandRefs
from boba.stand.site import ServiceRuntime, StandLayers
from boba.stand.ui.stand import REPO_ROOT, StandApp, StandPaths
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
    files = ConfigLocator.files()
    RawConfig.load(files)

    raw = StandLayers.compose(files)
    if not isinstance(raw, DictConfig):
        got = type(raw).__name__
        msg = f"stand config {files}: expected to compose into a table, got {got}"
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
    files = StandApp.MCP.files()
    raw = StandLayers.compose(files)
    if not isinstance(raw, DictConfig):
        got = type(raw).__name__
        msg = f"service config {files}: expected to compose into a table, got {got}"
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


@pytest.fixture(scope="session")
def bus_config(raw_config: DictConfig) -> RuntimeConfig:
    """Конфиг рантайма с шиной на Postgres для тестов самой шины: отладка
    выбирает в site.toml provider = local, а шину на Postgres тесты поднимают
    сами в тестовой базе."""
    raw = deepcopy(raw_config)
    OmegaConf.update(raw, f"env.{EnvOverride.MESSAGING.value}", "postgres")

    return bind(raw, path=RuntimeConfig.SECTION, model=RuntimeConfig)


@pytest.fixture(scope="session")
def service_runtime(service_raw_config: DictConfig) -> ServiceRuntime:
    """Конфиг процесса сервиса boba-mcp со стендовой базой тестов."""
    return bind(service_raw_config, path=ServiceRuntime.SECTION, model=ServiceRuntime)


@pytest.fixture(scope="session")
def process_config(runtime_config: RuntimeConfig) -> ProcessConfig:
    """Секции процесса набора: по умолчанию — приложения с браузером; набор
    над конфигом сервиса отдаёт service_runtime."""
    return runtime_config


@pytest.fixture(scope="session")
def stand_data_layer(runtime_config: RuntimeConfig) -> DataLayerConfig:
    """Сервер и схема базы тестов набора: по умолчанию — data layer
    приложения; набор над конфигом сервиса отдаёт слой стенда."""
    return runtime_config.data_layer


@pytest.fixture(scope="session", autouse=True)
def kerberos_workspace(
    process_config: ProcessConfig, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """Кэши билетов теста: тела инструментов ждут настроенный workspace."""
    from boba.krb import KerberosWorkspace  # noqa: PLC0415

    cache = tmp_path_factory.mktemp("krb-cache")
    KerberosWorkspace.configure(process_config.krb.config, str(cache))


@pytest.fixture(scope="session")
async def test_database(stand_data_layer: DataLayerConfig) -> str:
    return await TestDatabase.ensure(stand_data_layer.postgres)


@pytest.fixture
def test_postgres(
    stand_data_layer: DataLayerConfig, test_database: str
) -> PostgresConfig:
    """Профиль тестовой базы: тем, кто подключается сам, а не пулом."""
    return TestDatabase.config_of(stand_data_layer.postgres, test_database)


@pytest.fixture
async def pool(
    stand_data_layer: DataLayerConfig, test_database: str
) -> AsyncIterator[AsyncPostgresPool]:
    """Пул в тестовой базе с search_path на схему хранения приложения."""
    postgres = TestDatabase.config_of(stand_data_layer.postgres, test_database)
    p = AsyncPostgresPool(postgres.with_schema(stand_data_layer.db_schema))
    await p.open()
    try:
        yield p
    finally:
        await p.close()
