"""boba.config: загрузка TOML-конфига в omegaconf (ConfigLoader) и переход
секции в pydantic-модель (bind)."""

from boba.config.bind import bind
from boba.config.loader import ConfigError, ConfigLoader, ConfigResolver

__all__ = [
    "ConfigError",
    "ConfigLoader",
    "ConfigResolver",
    "bind",
]
