"""ConfigBuilder: явная сборка источников конфига в один инстанс (не глобал).

Приложение на bootstrap само перечисляет провайдеры-слои (YAML-файл, secrets,
CLI) и получает `DictConfig`-инстанс. Этот инстанс прокидывается в резолвер;
никакого процесс-глобала. Только стандартные средства OmegaConf: `load`
(YAML/JSON), `from_cli`, `merge`.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any, Self

from omegaconf import DictConfig, OmegaConf

__all__ = ["ConfigBuilder", "build_app_config"]

_CONFIG_PATH_ENV = "BOBA_CONFIG_PATH"
_SECRETS_PATH_ENV = "BOBA_SECRETS_PATH"


class ConfigBuilder:
    """Слои-источники → один `DictConfig`.

    Порядок добавления = приоритет: поздний слой перекрывает ранний при `merge`.
    """

    def __init__(self) -> None:
        self._layers: list[DictConfig] = []

    def add_yaml(self, path: str | Path | None) -> Self:
        """YAML/JSON-файл (нативный `OmegaConf.load`). None/отсутствие — no-op."""
        if path and Path(path).is_file():
            self._layers.append(OmegaConf.load(path))  # type: ignore[arg-type]
        return self

    def add_toml(self, path: str | Path | None) -> Self:
        """TOML-файл (`tomllib` → `OmegaConf.create`). None/отсутствие — no-op."""
        if path and Path(path).is_file():
            with Path(path).open("rb") as f:
                self._layers.append(OmegaConf.create(tomllib.load(f)))  # type: ignore[arg-type]
        return self

    def add_cli(self, argv: list[str] | None) -> Self:
        """CLI-токены `key=value` (`OmegaConf.from_cli`)."""
        if argv:
            self._layers.append(OmegaConf.from_cli(list(argv)))
        return self

    def add_dict(self, data: dict[str, Any]) -> Self:
        """In-memory слой (тесты / программные дефолты)."""
        self._layers.append(OmegaConf.create(dict(data)))  # type: ignore[arg-type]
        return self

    def build(self) -> DictConfig:
        if not self._layers:
            return OmegaConf.create({})  # type: ignore[return-value]

        return OmegaConf.merge(*self._layers)  # type: ignore[return-value]


def build_app_config(argv: list[str] | None = None) -> DictConfig:
    """Стандартная сборка приложения: config.toml + secrets.toml + CLI.

    Пути к файлам — из env (`$BOBA_CONFIG_PATH`, `$BOBA_SECRETS_PATH`); это
    только пути к источникам, не данные конфига. Возвращает инстанс — приложение
    держит его у себя и передаёт в резолвер.
    """
    return (
        ConfigBuilder()
        .add_toml(os.environ.get(_CONFIG_PATH_ENV))
        .add_toml(os.environ.get(_SECRETS_PATH_ENV))
        .add_cli(argv)
        .build()
    )
