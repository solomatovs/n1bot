"""TOML ConfigSource для Boba.

Public API::

    from boba.config.toml import TomlSource, TomlFileSource, load_toml
    from boba.config.toml import CONFIG_PATH_ENV, TOML_FILE_SUFFIX, toml_path

Подключение в bootstrap'е приложения::

    import os
    from boba.domain.core.config import ChainedConfigResolver
    from boba.config.toml import TomlSource, TomlFileSource, load_toml, CONFIG_PATH_ENV

    toml_data = load_toml(os.environ.get(CONFIG_PATH_ENV))
    resolver = ChainedConfigResolver([
        ...,
        TomlFileSource(toml_data),
        TomlSource(toml_data),
    ])
"""

from boba.config.toml.source import (
    CONFIG_PATH_ENV,
    TOML_FILE_SUFFIX,
    TomlFileSource,
    TomlSource,
    load_toml,
    toml_path,
)

__all__ = [
    "CONFIG_PATH_ENV",
    "TOML_FILE_SUFFIX",
    "TomlFileSource",
    "TomlSource",
    "load_toml",
    "toml_path",
]
