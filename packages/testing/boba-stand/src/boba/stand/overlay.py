"""Наложение теста на конфиг прогона: изменённые ключи слоем до разрешения.

Дерево ConfigLoader разрешено и только для чтения, ссылок в нём нет. Тест
поэтому не правит готовое дерево, а загружает файлы заново с наложением в
памяти последним слоем (ConfigLoader.load_over): ссылки на изменённые ключи
(env.data, env.tool_launcher, postgres.dbname) следуют за ними штатным
разрешением omegaconf.

Ошибки:
ConfigError — конфиг с наложением не загружается.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import ClassVar

from omegaconf import DictConfig, OmegaConf

from boba.config import ConfigLoader

__all__ = ["ConfigOverlay"]


class ConfigOverlay:
    """Файлы конфига прогона и постоянные изменения стенда (base); tree
    добавляет изменения теста. Ключ изменения — путь через точку, значение
    кладётся целиком (таблица сливается с таблицей конфига, список и скаляр
    заменяют прежние). Создают фикстуры стенда (config_overlay,
    service_overlay)."""

    REPLACED: ClassVar[str] = "stand_replaced"

    def __init__(self, loader: ConfigLoader, base: Mapping[str, object]) -> None:
        self._loader = loader
        self._base = dict(base)

    def tree(self, changes: Mapping[str, object]) -> DictConfig:
        overlay = OmegaConf.create()
        for key, value in self._base.items():
            OmegaConf.update(overlay, key, value, merge=False)

        for key, value in changes.items():
            OmegaConf.update(overlay, key, value, merge=False)

        return self._loader.load_over(overlay)

    def replacement(self, path: str, table: Mapping[str, object]) -> dict[str, object]:
        """Изменения, которые заменяют таблицу path целиком: слияние ключей не
        удаляет, поэтому таблица ложится в секцию REPLACED, а на её место —
        ссылка."""
        name = path.replace(".", "__")

        return {
            f"{self.REPLACED}.{name}": dict(table),
            path: f"${{{self.REPLACED}.{name}}}",
        }
