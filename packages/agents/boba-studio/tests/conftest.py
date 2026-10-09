"""Фикстуры тестов studio: конфиг studio из дерева отладки, пользователь стенда и
метка набора в именах стенда."""

from pathlib import Path

import pytest
from studio_stand import StandProfiles

from boba.config import bind
from boba.identity.api import AuthenticatedUser
from boba.runtime.config import RawConfig
from boba.stand.names import StandSuite
from boba.studio.config import StudioAppConfig

REPO = Path(__file__).resolve().parents[4]
STUDIO_CONFIG = REPO / "debug" / "studio" / "conf" / "config.toml"


def pytest_configure(config: pytest.Config) -> None:
    """Метка набора и процесса в именах стенда ставится до импорта модулей тестов."""
    StandSuite(config).configure("studio")


@pytest.fixture(scope="session")
def studio_config() -> StudioAppConfig:
    """Конфиг studio без побочных действий загрузчика."""
    raw = RawConfig.load(STUDIO_CONFIG)
    return bind(raw, path=StudioAppConfig.SECTION, model=StudioAppConfig)


@pytest.fixture
def user(studio_config: StudioAppConfig) -> AuthenticatedUser:
    return StandProfiles.user(studio_config)
