"""Базовые ConfigSource для Boba (boba.config-совместимый)."""

from boba.config.source.cli import (
    CliSource,
    parse_argv_path,
)
from boba.config.source.dict import DictSource
from boba.config.source.env import (
    ENV_FILE_SUFFIX,
    ENV_PREFIX,
    ENV_SEPARATOR,
    EnvFileSource,
    EnvSource,
)

__all__ = [
    "ENV_FILE_SUFFIX",
    "ENV_PREFIX",
    "ENV_SEPARATOR",
    "CliSource",
    "DictSource",
    "EnvFileSource",
    "EnvSource",
    "parse_argv_path",
]
