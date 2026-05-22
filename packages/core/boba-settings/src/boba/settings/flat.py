"""BobaFlatSettings: BaseSettings с рекурсивным flat→nested redistribute.

Идея: оператор пишет в TOML **плоскую** секцию, а pydantic-модель строится
со вложенными `BaseModel`-под-моделями произвольной глубины. Загрузчик
разворачивает плоские ключи по дереву аннотаций.

Пример:

    class PostgresConnectionConfig(BaseModel):
        host: str
        port: int = 5432

    class PostgresStoreConfig(BaseModel):
        connection: PostgresConnectionConfig
        schema: str = "public"

    class PostgresKnowledgeBaseConfig(BobaFlatSettings):
        model_config = BobaSettingsConfigDict(config_path="tool.kb.knowledge_base")
        store: PostgresStoreConfig
        rrf_k: int = 60

TOML:

    [tool.kb.knowledge_base]
    host = "127.0.0.1"      # → store.connection.host
    schema = "public"       # → store.schema
    rrf_k = 60              # → rrf_k

Конфликты имён (одно terminal-имя достижимо через несколько путей)
поднимаются как `ValueError` при инициализации класса (`__init_subclass__`),
то есть до загрузки конфига — fail-fast на этапе импорта модуля.

Источник конфиг-данных (TOML+env) живёт в `boba.settings.source` —
`BobaFlatSettings.settings_customise_sources` делегирует через
`ConfigSourcePydanticAdapter`.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, ClassVar, Self

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
    config_path   - путь к секции конфига (канонический путь).
    use_cli       - подключить `CliSettingsSource` (argparse интеграция).
    defaults_from - кортеж TOML-путей, из которых брать fallback-значения,
                    если ключ отсутствует в основной секции `config_path`.
                    Порядок задаёт приоритет fallback'ов (раньше — выше).
                    Локальная секция `config_path` всегда override'нёт shared.
                    Пример: `defaults_from=("postgres", "embedding")` для
                    KB-tool/CLI, чьи поля берутся из этих shared-секций.
    """

    config_path: str | tuple[str, ...]
    use_cli: bool
    defaults_from: tuple[str | tuple[str, ...], ...]


def _as_submodel(annotation: Any) -> type[BaseModel] | None:
    """Annotation → класс BaseModel-sub-model, иначе None."""
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return annotation

    return None


def _build_flat_key_paths(
    cls: type[BaseModel],
) -> dict[str, tuple[str, ...]]:
    """Собрать map `terminal-имя → путь` для всех листовых полей дерева.

    Терминальное поле — поле, чья аннотация **не** является `BaseModel`-под-моделью.
    Имена submodels'ов (промежуточные ноды) в map не попадают — они доступны
    как ключи в values только через явные nested-секции.

    Конфликт (одинаковое имя в разных submodel-ветках) → `ValueError`. Это
    проверка целостности схемы; вызывается один раз при первой нужде и
    кешируется в `cls.__dict__["_flat_key_paths"]`.
    """
    result: dict[str, tuple[str, ...]] = {}
    conflicts: dict[str, list[tuple[str, ...]]] = {}

    def walk(model: type[BaseModel], prefix: tuple[str, ...]) -> None:
        for fname, field in model.model_fields.items():
            sub = _as_submodel(field.annotation)
            path = (*prefix, fname)
            if sub is not None:
                walk(sub, path)
                continue
            existing = result.get(fname)
            if existing is not None and existing != path:
                conflicts.setdefault(fname, [existing]).append(path)
            else:
                result[fname] = path

    walk(cls, ())

    if conflicts:
        lines = []
        for key, paths in conflicts.items():
            formatted = " vs ".join(".".join(p) for p in paths)
            lines.append(f"  - {key!r}: {formatted}")
        msg = (
            f"{cls.__name__}: flat-key conflicts "
            "(same leaf-name reachable via multiple paths):\n"
            + "\n".join(lines)
        )
        raise ValueError(msg)

    return result


def _set_nested(
    target: dict[str, Any],
    path: tuple[str, ...],
    value: Any,
) -> None:
    """Записать value в target по path, создавая промежуточные dict'ы.

    Если на пути встречается non-dict (например, оператор передал готовый
    объект под этим ключом), он перезаписывается — flat-ключи имеют
    более низкий приоритет, чем явные nested-секции (см. `_deep_merge`
    в pass 2), поэтому перезапись здесь безопасна.
    """
    *parents, leaf = path
    cur = target
    for p in parents:
        existing = cur.get(p)
        if not isinstance(existing, dict):
            existing = {}
            cur[p] = existing
        cur = existing
    cur[leaf] = value


def _deep_merge(target: dict[str, Any], source: Mapping[str, Any]) -> None:
    """Рекурсивный merge: значения `source` overrides `target` на каждом уровне.

    Применяется, когда оператор смешивает flat-ключи и явную nested-секцию
    для одной submodel — явная секция перекрывает flat-значения.
    """
    for k, v in source.items():
        if isinstance(v, Mapping) and isinstance(target.get(k), dict):
            _deep_merge(target[k], v)
        else:
            target[k] = v


class BobaFlatSettings(BaseSettings):
    """
    Базовая модель конфигурирования pydantic-style:

        class AppConfig(BobaFlatSettings):
            model_config = BobaSettingsConfigDict(
                case_sensitive=False,
                extra="forbid",
                config_path="agent",
            )
            core:       AppCoreConfig   = Field(default_factory=AppCoreConfig)
            workspaces: WorkspaceLayout = Field(default_factory=WorkspaceLayout)
            ...

    Поддерживает произвольную вложенность под-моделей: TOML-секция остаётся
    плоской, validator раскладывает ключи по терминальным полям дерева
    `model_fields`. Конфликты имён ловятся в `__init_subclass__`.
    """

    # Кэш map `flat-name → path`, строится один раз на класс через
    # `__pydantic_init_subclass__` — pydantic-hook, который вызывается
    # ПОСЛЕ того как метакласс заполнил `model_fields`. Обычный
    # `__init_subclass__` не годится: к моменту его вызова `model_fields`
    # ещё не построен.
    _flat_key_paths: ClassVar[dict[str, tuple[str, ...]]] = {}

    @classmethod
    def __pydantic_init_subclass__(cls, **kwargs: Any) -> None:
        super().__pydantic_init_subclass__(**kwargs)
        # Fail-fast: конфликт имён ловится при импорте модуля,
        # а не при первом запуске.
        cls._flat_key_paths = _build_flat_key_paths(cls)

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

        flat_map = cls._flat_key_paths
        result: dict[str, Any] = {}
        leftover: dict[str, Any] = {}

        # Pass 1: flat-ключи разворачиваем по их пути в дереве.
        for k, v in values.items():
            if k in flat_map:
                _set_nested(result, flat_map[k], v)
            else:
                leftover[k] = v

        # Pass 2: всё остальное (явные nested-секции под именами submodel'ов
        # + неизвестные ключи). Nested-dict для уже существующей ветки
        # deep-merge'им — явная секция перекрывает flat-значения.
        for k, v in leftover.items():
            existing = result.get(k)
            if isinstance(v, Mapping) and isinstance(existing, dict):
                _deep_merge(existing, v)
            else:
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
        cli_enabled = bool(cfg.get("use_cli", False))
        defaults_from = cfg.get("defaults_from", ())

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

        # Shared TOML-source — один экземпляр на все adapter'ы, читает
        # `$BOBA_CONFIG_PATH` по требованию.
        toml_source = TomlEnvConfigSource()

        path = to_config_path(toml_section)
        if path:
            sources.append(
                ConfigSourcePydanticAdapter(
                    settings_cls,
                    source=toml_source,
                    path=path,
                ),
            )

        # Fallback-секции: каждая дочитывает только те ключи, которых нет
        # в более приоритетных source'ах. Pydantic-settings мержит state'ы
        # через deep_update — early-source-wins.
        for base in defaults_from:
            base_path = to_config_path(base)
            if not base_path:
                continue
            sources.append(
                ConfigSourcePydanticAdapter(
                    settings_cls,
                    source=toml_source,
                    path=base_path,
                ),
            )

        return tuple(sources)
