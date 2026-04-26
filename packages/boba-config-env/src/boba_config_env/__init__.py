"""Env-variable :class:`ConfigSource` для Boba.

Public API::

    from boba_config_env import EnvSource, EnvFileSource, env_name
    from boba_config_env import ENV_PREFIX, ENV_FILE_SUFFIX

Подключение в bootstrap'е приложения::

    from boba.domain.core.config import ChainedConfigResolver
    from boba_config_env import EnvSource, EnvFileSource

    resolver = ChainedConfigResolver([EnvFileSource(), EnvSource(), ...])
"""

from boba_config_env._source import (
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
