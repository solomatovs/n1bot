"""Фикстуры тестов сервиса boba-mcp: конфиг сервиса из дерева compose и метка
набора в именах стенда."""

import pytest
from omegaconf import DictConfig

from boba.config import bind
from boba.mcp_server.app import McpAppConfig
from boba.runtime.config import RuntimeConfig
from boba.stand.names import StandSuite


def pytest_configure(config: pytest.Config) -> None:
    """Метка набора и процесса в именах стенда ставится до импорта модулей тестов."""
    StandSuite(config).configure("mcp")


@pytest.fixture(scope="session")
def raw_config(service_raw_config: DictConfig) -> DictConfig:
    """Конфиг набора — конфиг сервиса: его секции инструментов и плагины."""
    return service_raw_config


@pytest.fixture(scope="session")
def runtime_config(raw_config: DictConfig) -> RuntimeConfig:
    """Конфиг рантайма сервиса: профилей и ролей чата у него нет."""
    return bind(raw_config, path=RuntimeConfig.SECTION, model=McpAppConfig)
