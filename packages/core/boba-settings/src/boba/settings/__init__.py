"""boba.settings: сборка конфига (builder), переход в pydantic (bind)."""

from boba.settings.bind import bind
from boba.settings.builder import ConfigBuilder, build_app_config

__all__ = [
    "ConfigBuilder",
    "bind",
    "build_app_config",
]
