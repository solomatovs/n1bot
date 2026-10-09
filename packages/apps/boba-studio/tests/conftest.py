"""Фикстуры тестов studio: общий конфиг studio и site.toml его дерева отладки,
пользователь стенда и метка набора в именах стенда."""

import pytest
from studio_stand import StandProfiles

from boba.config import bind
from boba.identity.api import AuthenticatedUser
from boba.runtime.config import RawConfig
from boba.stand.names import StandSuite
from boba.stand.ui.stand import StandApp
from boba.studio.config import StudioAppConfig


def pytest_configure(config: pytest.Config) -> None:
    """Метка набора и процесса в именах стенда ставится до импорта модулей тестов."""
    StandSuite(config).configure("studio")


@pytest.fixture(scope="session")
def studio_config() -> StudioAppConfig:
    """Конфиг studio без побочных действий загрузчика."""
    raw = RawConfig.load(StandApp.STUDIO.files())
    return bind(raw, path=StudioAppConfig.SECTION, model=StudioAppConfig)


@pytest.fixture
def user(studio_config: StudioAppConfig) -> AuthenticatedUser:
    return StandProfiles.user(studio_config)
