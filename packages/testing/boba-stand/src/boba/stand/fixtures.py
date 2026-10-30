"""Плагин pytest общего стенда: конфиг приложения, тестовая база и пул, kerberos.

Файлы конфигурации называет окружение: BOBA_CONFIG_PATH — главный конфиг и
наложения через os.pathsep (StandConfigPaths).
"""

from collections.abc import AsyncIterator, Iterator
from enum import StrEnum

import pytest
from omegaconf import DictConfig

from boba.config import ConfigLoader, bind
from boba.db.postgres import AsyncPostgresPool
from boba.db.postgres.connection import PostgresConfig
from boba.runtime.config import ProcessConfig, RawConfig, RuntimeConfig
from boba.stand.apps_slot import AppsDocker, AppsSlot
from boba.stand.database import TestDatabase
from boba.stand.overlay import ConfigOverlay
from boba.stand.refs import StandRefs
from boba.stand.site import StandConfigPaths
from boba.stand.ui.stand import StandApp
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


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        AppsDocker.OPTION.value,
        dest=AppsDocker.OPTION_DEST.value,
        default=AppsDocker.IMAGE.value,
        help=(
            "образ тестовых серверов MCP прежних редакций (make -C build testapps "
            "TESTAPPS_TAGS=<образ>); по умолчанию общий boba-testapps:latest"
        ),
    )


@pytest.fixture(scope="session")
def apps_slot(request: pytest.FixtureRequest) -> Iterator[AppsSlot]:
    """Контейнер серверов MCP редакции 2025-11-25 процесса тестов (образ
    boba-testapps либо названный --testapps-image); без образа тест падает
    с причиной."""
    image = str(request.config.getoption(AppsDocker.OPTION_DEST.value))
    slot = AppsSlot(image)
    try:
        slot.start()
        yield slot
    finally:
        slot.stop()


class ServiceData(StrEnum):
    """Подкаталоги каталога данных сервиса, которые он ждёт готовыми."""

    WORKSPACE = "workspace"
    TOOL_LOGS = "tool-logs"
    DUMP = "dump"
    KRB = "krb"


@pytest.fixture(scope="session")
def raw_config() -> DictConfig:
    """Конфиг прогона: файлы BOBA_CONFIG_PATH — конфиг приложения и наложение
    стенда stand.toml поверх; он же — дерево процесса (RawConfig)."""
    return RawConfig.load(StandConfigPaths().paths())


@pytest.fixture(scope="session")
def service_raw_config(tmp_path_factory: pytest.TempPathFactory) -> DictConfig:
    """Конфиг сервиса boba-mcp с наложением его stand.toml: секции
    инструментов и их плагины живут у сервиса, который инструменты исполняет.

    Корень — дерево сервиса, каким бы приложением ни был запущен прогон:
    модели плагинов лежат там. Каталог данных — временный: образы workspace,
    журналы и выгрузки тестов не должны ложиться в данные развёрнутого
    приложения."""
    raw = ConfigLoader(StandApp.MCP.paths()).load()

    data = tmp_path_factory.mktemp("service-data")
    for name in ServiceData:
        (data / name.value).mkdir()

    changes = {"instance.data_dir": str(data)}

    return ConfigOverlay(raw).merged(changes)


@pytest.fixture(scope="session")
def runtime_config(raw_config: DictConfig) -> RuntimeConfig:
    """Конфиг рантайма без побочных действий загрузчика: кэши kerberos ставит стенд."""
    return bind(raw_config, path=RuntimeConfig.SECTION, model=RuntimeConfig)


@pytest.fixture(scope="session")
def bus_config(raw_config: DictConfig) -> RuntimeConfig:
    """Конфиг рантайма с шиной на Postgres для тестов самой шины: отладка
    выбирает в конфиге provider = local, а шину на Postgres тесты поднимают
    сами в тестовой базе."""
    raw = ConfigOverlay(raw_config).merged({"env.messaging_provider": "postgres"})

    return bind(raw, path=RuntimeConfig.SECTION, model=RuntimeConfig)


@pytest.fixture(scope="session")
def service_runtime(service_raw_config: DictConfig) -> ProcessConfig:
    """Секции процесса сервиса boba-mcp: модель самого сервиса лежит в его
    пакете и пакетам инструментов недоступна по слоям."""
    return bind(service_raw_config, path=ProcessConfig.SECTION, model=ProcessConfig)


@pytest.fixture(scope="session")
def process_config(runtime_config: RuntimeConfig) -> ProcessConfig:
    """Секции процесса набора: по умолчанию — приложения с браузером; набор
    над конфигом сервиса отдаёт service_runtime."""
    return runtime_config


@pytest.fixture(scope="session")
def stand_postgres(raw_config: DictConfig) -> PostgresConfig:
    """Сервер базы тестов набора: подключение процесса [postgres] конфига
    набора — приложения или сервиса."""
    return bind(raw_config, path="postgres", model=PostgresConfig)


@pytest.fixture(scope="session", autouse=True)
def kerberos_workspace(
    process_config: ProcessConfig, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """Кэши билетов теста: тела инструментов ждут настроенный workspace;
    krb5.conf и keytab'ы — conf/krb рядом с главным конфигом прогона."""
    from boba.krb import KerberosWorkspace  # noqa: PLC0415

    conf_dir = StandConfigPaths().paths()[0].resolve().parent
    krb5_conf = process_config.instance.layout(conf_dir).krb5_conf()
    cache = tmp_path_factory.mktemp("krb-cache")
    KerberosWorkspace.configure(str(krb5_conf), str(cache))


@pytest.fixture(scope="session")
async def test_database(stand_postgres: PostgresConfig) -> str:
    return await TestDatabase.ensure(stand_postgres)


@pytest.fixture
def test_postgres(stand_postgres: PostgresConfig, test_database: str) -> PostgresConfig:
    """Профиль тестовой базы: тем, кто подключается сам, а не пулом."""
    return TestDatabase.config_of(stand_postgres, test_database)


@pytest.fixture
async def pool(
    stand_postgres: PostgresConfig, test_database: str
) -> AsyncIterator[AsyncPostgresPool]:
    """Пул в тестовой базе: таблицы квалифицируются схемой в запросах."""
    p = AsyncPostgresPool(TestDatabase.config_of(stand_postgres, test_database))
    await p.open()
    try:
        yield p
    finally:
        await p.close()
