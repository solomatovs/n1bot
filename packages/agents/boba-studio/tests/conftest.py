"""Фикстуры тестов studio: конфиг studio из дерева compose и пользователь стенда."""

from pathlib import Path

import pytest
from studio_stand import StandProfiles

from boba.config import bind
from boba.identity.api import AuthenticatedUser
from boba.runtime.config import RawConfig, StudioRuntimeConfig

REPO = Path(__file__).resolve().parents[4]
STUDIO_CONFIG = REPO / "compose" / "studio" / "conf" / "config.toml"


@pytest.fixture(scope="session")
def studio_config() -> StudioRuntimeConfig:
    """Конфиг studio без побочных действий загрузчика."""
    raw = RawConfig.load(STUDIO_CONFIG)
    return bind(raw, path=StudioRuntimeConfig.SECTION, model=StudioRuntimeConfig)


@pytest.fixture
def user(studio_config: StudioRuntimeConfig) -> AuthenticatedUser:
    return StandProfiles.user(studio_config)
