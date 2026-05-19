"""BobaFlatSettings: BaseSettings с flat→nested redistribute.

Чистая pydantic-schema. Источник конфиг-данных (TOML+env) живёт в
`boba.settings.source` — `BobaFlatSettings.settings_customise_sources`
делегирует через `ConfigSourcePydanticAdapter`.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Self

from pydantic import BaseModel, model_validator
from pydantic_settings import (
    BaseSettings,
    CliSettingsSource,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

from boba.settings.source import (
    ConfigSourcePydanticAdapter,
    TomlEnvConfigSource,
    to_config_path,
)

__all__ = [
    "BobaFlatSettings",
    "BobaSettingsConfigDict",
]


class BobaSettingsConfigDict(SettingsConfigDict, total=False):
    """
    config_path - путь к секции конфига (канонический путь)
    use_cli     - подключить `CliSettingsSource` (argparse интеграция)
    """

    config_path: str | tuple[str, ...]
    use_cli: bool


def _as_submodel(annotation: Any) -> type[BaseModel] | None:
    """Annotation → класс BaseModel-sub-model, иначе None."""
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return annotation

    return None


def _collect_subfield(
    name: str,
    sub_model: type[BaseModel],
    values: Mapping[str, Any],
    used: set[str],
) -> dict[str, Any] | Any | None:
    """
    Собрать значение для sub-model

    Возвращает:
      * dict — собранное содержимое sub-model (flat-keys nested-overlay);
      * не-dict — passthrough, если на месте `name` лежал готовый объект;
      * None — для поля нечего собрать (использовать field's default).
    """
    sub_dict: dict[str, Any] = {}
    for sub_field in sub_model.model_fields:
        if sub_field in values:
            sub_dict[sub_field] = values[sub_field]
            used.add(sub_field)

    existing = values.get(name)
    if isinstance(existing, Mapping):
        merged: dict[str, Any] = dict(sub_dict)
        merged.update(existing)
        return merged

    if sub_dict:
        return sub_dict

    if name in values:
        return values[name]

    return None


class BobaFlatSettings(BaseSettings):
    """
    Базовая модель конфигурирования pydentic style:

        class AppConfig(BobaFlatSettings):
            model_config = BobaSettingsConfigDict(
                case_sensitive=False,
                extra="forbid",
                config_path="agent",
            )
            core:       AppCoreConfig   = Field(default_factory=AppCoreConfig)
            workspaces: WorkspaceLayout = Field(default_factory=WorkspaceLayout)
            ...
    """

    @classmethod
    def load(cls) -> Self:
        """
        Сконструировать DTO из сконфигурированных source
        """
        return cls()  # pyright: ignore[reportCallIssue]

    @model_validator(mode="before")
    @classmethod
    def _redistribute_flat_keys(cls, values: Any) -> Any:
        if not isinstance(values, Mapping):
            return values

        result: dict[str, Any] = {}
        used: set[str] = set()
        for name, field in cls.model_fields.items():
            sub_model = _as_submodel(field.annotation)
            if sub_model is None:
                continue

            entry = _collect_subfield(name, sub_model, values, used)
            if entry is not None:
                result[name] = entry

            used.add(name)

        for k, v in values.items():
            if k not in used:
                result[k] = v

        return result

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        del env_settings, dotenv_settings, file_secret_settings
        cfg = settings_cls.model_config
        toml_section = cfg.get("config_path")
        cli_enabled = bool(cfg.get("boba_cli", False))

        sources: list[PydanticBaseSettingsSource] = []
        if cli_enabled:
            sources.append(
                CliSettingsSource(
                    settings_cls,
                    cli_parse_args=True,
                    cli_ignore_unknown_args=False,
                    cli_kebab_case=True,
                ),
            )

        sources.append(init_settings)

        path = to_config_path(toml_section)
        if path:
            sources.append(
                ConfigSourcePydanticAdapter(
                    settings_cls,
                    source=TomlEnvConfigSource(),
                    path=path,
                ),
            )
        return tuple(sources)
