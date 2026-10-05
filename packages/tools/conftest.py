"""Фикстуры тестов плагинов инструментов: конфиг сервиса boba-mcp, который
инструменты исполняет — его секции [tool.<id>] и общие секции рантайма."""

import pytest
from omegaconf import DictConfig

from boba.config import bind
from boba.runtime.config import RuntimeConfig
from boba.stand.site import ServiceRuntime


@pytest.fixture(scope="session")
def raw_config(service_raw_config: DictConfig) -> DictConfig:
    """Конфиг набора — конфиг сервиса со стендовым слоем."""
    return service_raw_config


@pytest.fixture(scope="session")
def runtime_config(raw_config: DictConfig) -> RuntimeConfig:
    """Конфиг рантайма сервиса: профилей и ролей чата у него нет."""
    return bind(raw_config, path=RuntimeConfig.SECTION, model=ServiceRuntime)
