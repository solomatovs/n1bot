"""boba.settings: общая база pydantic-settings.

Точки входа:
- `BobaFlatSettings` — `BaseSettings`-наследник с двумя готовыми вещами:
    * `model_validator(mode='before')`, который flat-input распределяет по
      nested BaseModel-полям (`Inline()`-семантика старого boba.schema).
    * `settings_customise_sources` по умолчанию читает конфиг через
      `BobaSettingsConfigDict`-параметры (env-префикс + TOML-секция).
- `BobaSettingsConfigDict` — расширение `SettingsConfigDict` с boba-специфичными
   полями `boba_env_prefix`, `boba_env_delimiter`, `boba_toml_path_env`,
   `boba_toml_section`.
- `PrefixedFlatEnvSource`, `SectionTomlSource` — публичные source-классы,
   полезные если нужен кастомный `settings_customise_sources`.
"""

from boba.settings.flat import (
    BobaFlatSettings,
    BobaSettingsConfigDict,
    PrefixedFlatEnvSource,
    SectionTomlSource,
)

__all__ = [
    "BobaFlatSettings",
    "BobaSettingsConfigDict",
    "PrefixedFlatEnvSource",
    "SectionTomlSource",
]
