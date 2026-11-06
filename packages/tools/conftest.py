"""Фикстуры тестов плагинов инструментов: конфиг сервиса boba-mcp, который
инструменты исполняет — его секции [tool.<id>] и общие секции рантайма."""

import pytest
from omegaconf import DictConfig

from boba.mcp_server.config.models import McpAppConfig
from boba.stand.overlay import ConfigOverlay


@pytest.fixture(scope="session")
def raw_config(service_raw_config: DictConfig) -> DictConfig:
    """Конфиг набора — конфиг сервиса со стендовым слоем."""
    return service_raw_config


@pytest.fixture(scope="session")
def config_overlay(service_overlay: ConfigOverlay) -> ConfigOverlay:
    """Наложения набора ложатся на конфиг сервиса, как и raw_config."""
    return service_overlay


@pytest.fixture(scope="session")
def process_config(service_config: McpAppConfig) -> McpAppConfig:
    """Секции процесса набора — сервиса: профилей и ролей чата у него нет."""
    return service_config
