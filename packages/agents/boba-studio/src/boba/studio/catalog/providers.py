"""Провайдеры каталога данных в контейнере studio: хранилища на старте,
сервис над ними, шиной и портами синхронизации; ссылка на сервис для
маршрутов и инструментов, которые зовут его на каждый запрос. Аннотации
живые: контейнер читает Depends из Annotated.

Ошибки:
RuntimeError — контейнер не поднят или конфиг не studio.
ServiceDisabledError — сервис каталога запрошен при выключенном [catalog].
"""

from typing import Annotated

from boba.catalog import SourceKinds
from boba.catalog_service import (
    CatalogConfig,
    CatalogService,
    ConnectionStore,
    ProcessStore,
    RegistrySyncTools,
    SyncPorts,
)
from boba.connection_broker.service import UserConnectionsService
from boba.messaging import MessageBus
from boba.runtime import providers as runtime
from boba.runtime.config import RuntimeConfig
from boba.runtime.di import Container, Depends
from boba.studio.catalog.sync_ports import BrokerConnectionDirectory
from boba.studio.config import StudioAppConfig

__all__ = [
    "catalog_config",
    "catalog_connections",
    "catalog_processes",
    "catalog_service",
    "catalog_service_ref",
    "studio_config",
]


def studio_config(
    config: Annotated[RuntimeConfig, Depends(runtime.get_runtime_config)],
) -> StudioAppConfig:
    if not isinstance(config, StudioAppConfig):
        msg = (
            "catalog providers expect StudioAppConfig from the runtime config "
            f"provider, got {type(config).__name__}"
        )
        raise RuntimeError(msg)

    return config


def catalog_config(
    config: Annotated[StudioAppConfig, Depends(studio_config)],
) -> CatalogConfig:
    return config.catalog


async def catalog_processes(
    cfg: Annotated[CatalogConfig, Depends(catalog_config)],
) -> ProcessStore | None:
    """Хранилище процессов с таблицами на старте; None — секция [catalog] выключена."""
    if not cfg.enable:
        return None

    processes = ProcessStore(cfg)
    await processes.setup()

    return processes


async def catalog_connections(
    cfg: Annotated[CatalogConfig, Depends(catalog_config)],
) -> ConnectionStore | None:
    """Хранилище снимков подключений; None — секция выключена."""
    if not cfg.enable:
        return None

    # снимки видов подключений приносят пакеты-владельцы драйверов
    connections = ConnectionStore(cfg, SourceKinds.discover())
    await connections.setup()

    return connections


def catalog_service(
    processes: Annotated[ProcessStore | None, Depends(catalog_processes)],
    connections: Annotated[ConnectionStore | None, Depends(catalog_connections)],
    cfg: Annotated[CatalogConfig, Depends(catalog_config)],
    bus: Annotated[MessageBus, Depends(runtime.message_bus)],
) -> CatalogService | None:
    """Сервис каталога над хранилищами и шиной процесса."""
    if processes is None:
        return None

    if connections is None:
        return None

    tools = RegistrySyncTools(runtime.tool_registry_ref)
    names = BrokerConnectionDirectory(
        UserConnectionsService(runtime.connection_store_ref)
    )
    ports = SyncPorts(tools, names)

    return CatalogService(processes, connections, cfg, bus, ports)


async def catalog_service_ref() -> CatalogService:
    """Сервис каталога из корневого контейнера; зовётся на каждый запрос."""
    root = Container.require_root("catalog_service_ref")
    service = await root.resolve(Depends(catalog_service))

    return runtime.required(service, "catalog", "the data catalog")
