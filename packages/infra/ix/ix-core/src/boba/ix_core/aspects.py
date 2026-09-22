"""Аспекты: классы, строка словаря {schema}.aspect, строка объявления
{schema}.surface_aspect и контракт тела объявления.

Потребитель (индексатор, описатель) подписан на классы аспектов; объявления своих
классов и их `union all`-источник для файлов run/ он берёт у IxRegistry, контракт
тела проверяет накат схемы (SchemaUpgrade).

Ошибки:
AspectDeclarationError — тело объявления не выполняется на этой базе или
    отдаёт не те колонки, что требует контракт (node_id bigint, content varchar).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

__all__ = [
    "AspectClass",
    "AspectDeclarationError",
    "AspectEntry",
    "ContractColumn",
    "ContractType",
    "SurfaceAspect",
]


class AspectDeclarationError(Exception):
    """Объявление аспекта не отвечает контракту."""


class AspectClass(StrEnum):
    """Класс аспекта: что это за текст; значения enum aspect_class_e."""

    IDENT = "ident"
    WORDS = "words"
    DESCRIPTION = "description"
    DESCRIBER_INPUT = "describer_input"


class ContractColumn(StrEnum):
    """Колонки, которые обязано вернуть тело объявления, в этом порядке."""

    NODE_ID = "node_id"
    CONTENT = "content"


class ContractType(StrEnum):
    """Имена типов postgres, допустимых для колонок контракта."""

    INT8 = "int8"
    TEXT = "text"
    VARCHAR = "varchar"


@dataclass(frozen=True, kw_only=True)
class AspectEntry:
    """Одна строка словаря {schema}.aspect: имя, класс, описание и владелец."""

    aspect: str
    aspect_class: AspectClass
    description: str
    owner: str


@dataclass(frozen=True, kw_only=True)
class SurfaceAspect:
    """Одна строка {schema}.surface_aspect."""

    surface: str
    aspect: str
    body: str
