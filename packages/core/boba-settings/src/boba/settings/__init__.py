"""boba.settings: OmegaConf-источники + чистые pydantic-модели.

Разделение ответственности:

- `boba.settings.builder` — явная сборка источников в инстанс (`ConfigBuilder`,
  `build_app_config`). Не глобал: приложение держит инстанс и передаёт дальше.
- `boba.settings.bind` — единственная точка `OmegaConf → pydantic` (`bind`).
- `boba.settings.resolver` — реализации портов `boba.tools` поверх конфиг-инстанса;
  секция плагина = `tool.<plugin_name>` (`plugin_section`), путь даёт сборщик.
- `boba.settings.types` — переиспользуемые pydantic-типы (`StringList`).

Модель про своё место в конфиге не знает; OmegaConf скрыт внутри пакета —
потребители работают с pydantic-моделями через `bind`/`ConfigResolver`.
"""

from boba.settings.bind import bind
from boba.settings.builder import ConfigBuilder, build_app_config
from boba.settings.resolver import (
    OmegaConfPluginToolFilter,
    OmegaConfResolver,
    PluginConfigBase,
    plugin_section,
)
from boba.settings.types import LLMStringList, StringList

__all__ = [
    "ConfigBuilder",
    "LLMStringList",
    "OmegaConfPluginToolFilter",
    "OmegaConfResolver",
    "PluginConfigBase",
    "StringList",
    "bind",
    "build_app_config",
    "plugin_section",
]
