"""Наложение на загруженное дерево конфига в памяти теста.

Дерево ConfigLoader только для чтения: тест не правит его, а кладёт поверх
изменённые ключи штатным OmegaConf.merge. Ссылки дерева остаются ссылками,
поэтому смена env.data или postgres.dbname доходит до всех секций, которые на
них ссылаются. Слияние не удаляет ключей: таблицу, из которой нужно убрать
ключ, replaced кладёт целиком в секцию REPLACED и ставит на её место ссылку.

Ошибки:
TypeError — наложение не дало таблицы.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import ClassVar

from omegaconf import DictConfig, OmegaConf

__all__ = ["ConfigOverlay"]


class ConfigOverlay:
    """Ключи теста поверх дерева конфига: ключ — путь через точку, значение
    кладётся целиком (таблица сливается с таблицей дерева, список и скаляр
    заменяют прежние). Создаёт тест или фикстура над raw_config."""

    REPLACED: ClassVar[str] = "stand_replaced"

    def __init__(self, raw: DictConfig) -> None:
        self._raw = raw

    def merged(self, changes: Mapping[str, object]) -> DictConfig:
        overlay = OmegaConf.create()
        for key, value in changes.items():
            OmegaConf.update(overlay, key, value, merge=False)

        merged = OmegaConf.merge(self._raw, overlay)
        if not isinstance(merged, DictConfig):
            got = type(merged).__name__
            msg = f"config overlay {sorted(changes)}: expected a table, got {got}"
            raise TypeError(msg)

        return merged

    def replaced(self, path: str, table: Mapping[str, object]) -> DictConfig:
        """Таблица path заменена table целиком: её прежние ключи уходят."""
        name = path.replace(".", "__")
        changes: dict[str, object] = {
            f"{self.REPLACED}.{name}": dict(table),
            path: f"${{{self.REPLACED}.{name}}}",
        }

        return self.merged(changes)
