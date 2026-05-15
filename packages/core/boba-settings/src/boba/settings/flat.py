"""BobaFlatSettings: BaseSettings с flat→nested redistribute и boba sources."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Self

from pydantic import BaseModel, model_validator
from pydantic.fields import FieldInfo
from pydantic_settings import (
    BaseSettings,
    CliSettingsSource,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    TomlConfigSettingsSource,
)

__all__ = [
    "BobaFlatSettings",
    "BobaSettingsConfigDict",
    "PrefixedFlatEnvSource",
    "SectionTomlSource",
]


_DEFAULT_ENV_DELIMITER = "__"
_DEFAULT_TOML_PATH_ENV = "BOBA_CONFIG_PATH"


class BobaSettingsConfigDict(SettingsConfigDict, total=False):
    """`SettingsConfigDict` + boba-специфичные ключи.

    boba_env_prefix:    env-префикс источника (например, "BOBA_AGENT__").
                         None/пусто — env-source не подключается.
    boba_env_delimiter: разделитель сегментов nested-env (по умолчанию "__").
    boba_toml_path_env: имя env-варса, содержащего путь к TOML-файлу.
                         По умолчанию "BOBA_CONFIG_PATH".
    boba_toml_section:  top-level секция TOML (например, "agent").
                         None/пусто — TOML-source не подключается.
    boba_cli:           True — подключить `CliSettingsSource` с
                         `cli_parse_args=True` и `cli_ignore_unknown_args=True`
                         (поля становятся флагами argparse). По умолчанию False.
    """

    boba_env_prefix: str
    boba_env_delimiter: str
    boba_toml_path_env: str
    boba_toml_section: str
    boba_cli: bool


class PrefixedFlatEnvSource(PydanticBaseSettingsSource):
    """Все env-варсы с префиксом `<PREFIX>` → flat/nested dict.

    Один сегмент после префикса → flat-ключ:
      `BOBA_AGENT__BASE_URL=…` → `{base_url: …}`.
    Несколько сегментов через `<DELIMITER>` → nested:
      `BOBA_AGENT__OPENAI__BASE_URL=…` → `{openai: {base_url: …}}`.

    Распределение flat-ключей по полям sub-моделей делает
    `BobaFlatSettings._redistribute_flat_keys` (model_validator).
    """

    def __init__(
        self,
        settings_cls: type[BaseSettings],
        prefix: str,
        delimiter: str = _DEFAULT_ENV_DELIMITER,
    ) -> None:
        super().__init__(settings_cls)
        self._prefix = prefix
        self._delimiter = delimiter

    def get_field_value(
        self,
        field: FieldInfo,
        field_name: str,
    ) -> tuple[Any, str, bool]:
        # Не используется: __call__ возвращает целиком собранный dict.
        del field
        return None, field_name, False

    def __call__(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key, value in os.environ.items():
            if not key.startswith(self._prefix):
                continue
            tail = key[len(self._prefix):]
            parts = [p.lower() for p in tail.split(self._delimiter) if p]
            if not parts:
                continue
            cursor: dict[str, Any] = out
            for part in parts[:-1]:
                child = cursor.get(part)
                if not isinstance(child, dict):
                    child = {}
                    cursor[part] = child
                cursor = child
            cursor[parts[-1]] = value
        return out


class SectionTomlSource(TomlConfigSettingsSource):
    """`TomlConfigSettingsSource`, читающий только заданную top-level секцию."""

    def __init__(
        self,
        settings_cls: type[BaseSettings],
        toml_file: Path | str,
        section: str,
    ) -> None:
        self._section = section
        super().__init__(settings_cls, toml_file=toml_file)

    def _read_file(self, file_path: Path) -> dict[str, Any]:
        full = super()._read_file(file_path)
        section = full.get(self._section, {})
        return section if isinstance(section, dict) else {}


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
    """Собрать значение для sub-model-поля из flat- и/или nested-input'а.

    Возвращает:
      * dict — собранное содержимое sub-model (flat-keys ∪ nested-overlay);
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
    """`BaseSettings` с flat→nested redistribute и стандартными boba-source'ами.

    Subclass-конвенция:

        class AppConfig(BobaFlatSettings):
            model_config = BobaSettingsConfigDict(
                case_sensitive=False,
                extra="forbid",
                boba_env_prefix="BOBA_AGENT__",
                boba_toml_section="agent",
            )
            core:       AppCoreConfig   = Field(default_factory=AppCoreConfig)
            workspaces: WorkspaceLayout = Field(default_factory=WorkspaceLayout)
            ...

    На вход принимает:
      * flat-dict: `{log_level: "INFO", base_url: "..."}` — model-validator
        раскладывает ключи по полям sub-моделей через introspection
        `cls.model_fields`.
      * nested-dict: `{core: {log_level: "..."}, openai: {...}}` — пробрасывается
        как есть; при пересечении с flat-формой nested выигрывает.

    Источники (priority high → low):
      1. CLI argv (если `boba_cli=True`).
      2. init-kwargs (`AppConfig(core=...)`).
      3. env: `<boba_env_prefix><FIELD>` (или nested через delimiter).
      4. TOML: top-level секция `[<boba_toml_section>]` в файле
         `$<boba_toml_path_env>` (по умолчанию `$BOBA_CONFIG_PATH`).
    """

    @classmethod
    def load(cls) -> Self:
        """Сконструировать инстанс из сконфигурированных source'ов.

        Pyright/Pylance не понимают, что `BaseSettings()` без аргументов
        заполняет required-поля из source'ов, и помечают вызов как
        `reportCallIssue: missing argument`. `load()` локализует
        type-ignore здесь, оставляя call-сайты чистыми.
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
        prefix = cfg.get("boba_env_prefix")
        delimiter = cfg.get("boba_env_delimiter", _DEFAULT_ENV_DELIMITER)
        toml_path_env = cfg.get("boba_toml_path_env", _DEFAULT_TOML_PATH_ENV)
        toml_section = cfg.get("boba_toml_section")
        cli_enabled = bool(cfg.get("boba_cli", False))

        sources: list[PydanticBaseSettingsSource] = []
        if cli_enabled:
            sources.append(
                CliSettingsSource(
                    settings_cls,
                    cli_parse_args=True,
                    cli_ignore_unknown_args=True,
                    cli_kebab_case=True,
                ),
            )
        sources.append(init_settings)
        if prefix:
            sources.append(
                PrefixedFlatEnvSource(settings_cls, prefix, delimiter),
            )
        if toml_section:
            toml_path = os.environ.get(toml_path_env)
            if toml_path and Path(toml_path).is_file():
                sources.append(
                    SectionTomlSource(settings_cls, toml_path, toml_section),
                )
        return tuple(sources)
