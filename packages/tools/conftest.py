"""Фикстуры тестов плагинов инструментов: конфиг сервиса boba-mcp, который
инструменты исполняет — его секции [tool.<id>] и общие секции рантайма."""

import pytest
from omegaconf import DictConfig

from boba.runtime.config import DataLayerConfig, ProcessConfig
from boba.stand.site import ServiceRuntime


@pytest.fixture(scope="session")
def raw_config(service_raw_config: DictConfig) -> DictConfig:
    """Конфиг набора — конфиг сервиса со стендовым слоем."""
    return service_raw_config


@pytest.fixture(scope="session")
def process_config(service_runtime: ServiceRuntime) -> ProcessConfig:
    """Секции процесса набора — сервиса: профилей и ролей чата у него нет."""
    return service_runtime


@pytest.fixture(scope="session")
def stand_data_layer(service_runtime: ServiceRuntime) -> DataLayerConfig:
    """База тестов набора — из стендового слоя конфига сервиса."""
    return service_runtime.data_layer
