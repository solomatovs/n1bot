"""Фикстуры тестов сервиса boba-mcp: конфиг сервиса из дерева compose и метка
набора в именах стенда."""

import pytest
from omegaconf import DictConfig

from boba.runtime.config import ProcessConfig
from boba.stand.names import StandSuite


def pytest_configure(config: pytest.Config) -> None:
    """Метка набора и процесса в именах стенда ставится до импорта модулей тестов."""
    StandSuite(config).configure("mcp")


@pytest.fixture(scope="session")
def raw_config(service_raw_config: DictConfig) -> DictConfig:
    """Конфиг набора — конфиг сервиса: его секции инструментов и плагины."""
    return service_raw_config


@pytest.fixture(scope="session")
def process_config(service_runtime: ProcessConfig) -> ProcessConfig:
    """Секции процесса набора — сервиса: профилей и ролей чата у него нет."""
    return service_runtime
