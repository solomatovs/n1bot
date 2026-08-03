"""boba.settings: OmegaConf-источники + чистые pydantic-модели.

Разделение ответственности:

- boba.settings.builder — явная сборка источников в инстанс (ConfigBuilder,
  build_app_config). Не глобал: приложение держит инстанс и передаёт дальше.
- boba.settings.bind — единственная точка OmegaConf -> pydantic (bind).
- boba.settings.types — переиспользуемые pydantic-типы (StringList).

Модель про своё место в конфиге не знает; OmegaConf скрыт внутри пакета —
потребители работают с pydantic-моделями через bind.
"""

from boba.settings.bind import bind
from boba.settings.builder import ConfigBuilder, build_app_config
from boba.settings.types import LLMStringList, StringList

__all__ = [
    "ConfigBuilder",
    "LLMStringList",
    "StringList",
    "bind",
    "build_app_config",
]
