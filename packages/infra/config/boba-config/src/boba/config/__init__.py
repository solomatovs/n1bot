"""boba.config: сборка конфига (builder), переход в pydantic (bind)."""

from boba.config.bind import bind
from boba.config.builder import ConfigBuilder, build_app_config

__all__ = [
    "ConfigBuilder",
    "bind",
    "build_app_config",
]
