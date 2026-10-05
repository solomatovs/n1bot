"""Фикстуры тестов сервиса boba-mcp: конфиг сервиса из дерева compose и метка
набора в именах стенда."""

import pytest
from omegaconf import DictConfig

from boba.runtime.config import DataLayerConfig, ProcessConfig
from boba.stand.names import StandSuite
from boba.stand.site import ServiceRuntime


def pytest_configure(config: pytest.Config) -> None:
    """Метка набора и процесса в именах стенда ставится до импорта модулей тестов."""
    StandSuite(config).configure("mcp")


@pytest.fixture(scope="session")
def raw_config(service_raw_config: DictConfig) -> DictConfig:
    """Конфиг набора — конфиг сервиса: его секции инструментов и плагины."""
    return service_raw_config


@pytest.fixture(scope="session")
def process_config(service_runtime: ServiceRuntime) -> ProcessConfig:
    """Секции процесса набора — сервиса: профилей и ролей чата у него нет."""
    return service_runtime


@pytest.fixture(scope="session")
def stand_data_layer(service_runtime: ServiceRuntime) -> DataLayerConfig:
    """База тестов набора — из стендового слоя конфига сервиса."""
    return service_runtime.data_layer
