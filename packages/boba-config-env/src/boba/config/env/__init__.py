"""Env-variable ConfigSource для Boba.

Public API::

    from boba.config.env import EnvSource, EnvFileSource, env_name
    from boba.config.env import ENV_PREFIX, ENV_FILE_SUFFIX

Подключение в bootstrap'е приложения::

    from boba.domain.core.config import ChainedConfigResolver
    from boba.config.env import EnvSource, EnvFileSource

    resolver = ChainedConfigResolver([EnvFileSource(), EnvSource(), ...])
"""

from boba.config.env.source import (
    ENV_FILE_SUFFIX,
    ENV_PREFIX,
    EnvFileSource,
    EnvSource,
    env_name,
)

__all__ = [
    "ENV_FILE_SUFFIX",
    "ENV_PREFIX",
    "EnvFileSource",
    "EnvSource",
    "env_name",
]
