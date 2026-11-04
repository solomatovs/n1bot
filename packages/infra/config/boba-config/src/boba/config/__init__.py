"""boba.config: загрузка TOML-конфига в omegaconf (ConfigLoader) и переход
секции в pydantic-модель (bind)."""

from boba.config.bind import bind
from boba.config.loader import ConfigError, ConfigLoader, ConfigResolver
from boba.config.schema import ConfigModel, ConfigSchema, ConfigVariant

__all__ = [
    "ConfigError",
    "ConfigLoader",
    "ConfigModel",
    "ConfigResolver",
    "ConfigSchema",
    "ConfigVariant",
    "bind",
]
