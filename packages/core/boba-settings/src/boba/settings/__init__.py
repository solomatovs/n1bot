"""boba.settings: единый ConfigSource + pydantic-schema с flat→nested.

Чёткое разделение ответственности:

- `boba.settings.source` — ИСТОЧНИК конфиг-данных:
    * `ConfigPath` — каноническое представление ключа конфиг-секции.
    * `ConfigSource` Protocol — единый контракт `for_path(path) -> dict`.
    * `TomlEnvConfigSource` — default-реализация (`$BOBA_CONFIG_PATH` + `os.environ`).
    * `DictConfigSource` — in-memory, для тестов.
    * `ConfigSourcePydanticAdapter` — мост к pydantic-settings.
    * `to_config_path` — utility для нормализации ключа.

- `boba.settings.flat` — SCHEMA:
    * `BobaFlatSettings` — `BaseSettings`-наследник с flat→nested redistribute.
       Source-чтение делегирует через `ConfigSourcePydanticAdapter`.
    * `BobaSettingsConfigDict` — расширение `SettingsConfigDict` с
       schema-уровневыми полями `config_path` и `boba_cli`.
"""

from boba.settings.flat import (
    BobaFlatSettings,
    BobaSettingsConfigDict,
)
from boba.settings.source import (
    ConfigPath,
    ConfigSource,
    ConfigSourcePydanticAdapter,
    DictConfigSource,
    TomlEnvConfigSource,
    to_config_path,
)
from boba.settings.types import StringList

__all__ = [
    "BobaFlatSettings",
    "BobaSettingsConfigDict",
    "ConfigPath",
    "ConfigSource",
    "ConfigSourcePydanticAdapter",
    "DictConfigSource",
    "StringList",
    "TomlEnvConfigSource",
    "to_config_path",
]
