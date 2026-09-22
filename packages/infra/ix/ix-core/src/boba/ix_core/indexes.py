"""Реестр таблиц поисковых индексов: какие таблицы какого вида есть в схеме.

Таблицу создаёт её владелец, а поиск не знает имени: он берёт таблицы нужного вида из
IxRegistry и опрашивает их одним запросом. Строки вписывают владельцы файлами схемы,
накат (SchemaUpgrade) проверяет, что таблица существует и несёт колонки вида.

В реестре только вид, имя и владелец. Специфика вида — модель эмбеддинга и её
размерность у вектора, языковая конфигурация у полнотекста — живёт у владельца:
ядру она не нужна, а копия чужого конфига здесь только разошлась бы с оригиналом.

Ошибки:
IndexTableError — зарегистрированная таблица отсутствует или не несёт колонок,
    которых требует её вид.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from psycopg import sql

__all__ = [
    "IndexKind",
    "IndexTable",
    "IndexTableError",
    "index_columns",
]


class IndexTableError(Exception):
    """Таблица индекса не отвечает контракту своего вида."""


class IndexKind(StrEnum):
    """Вид поискового индекса; значения index_kind_e."""

    TRGM = "trgm"
    FTS = "fts"
    VECTOR = "vector"


@dataclass(frozen=True, kw_only=True)
class IndexTable:
    """Одна строка {schema}.index_table: таблица индекса и её вид."""

    kind: IndexKind
    name: str
    owner: str

    def ident(self) -> sql.Identifier:
        return sql.Identifier(self.name)


def index_columns(kind: IndexKind) -> tuple[str, ...]:
    """Колонки, которые обязана нести таблица индекса каждого вида."""
    common = ("node_id", "surface", "aspect", "content")
    by_kind = {
        IndexKind.TRGM: common,
        IndexKind.FTS: (*common, "tsv"),
        IndexKind.VECTOR: (*common, "chunk_no", "content_hash", "emb"),
    }

    return by_kind[kind]
