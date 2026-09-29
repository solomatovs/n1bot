"""Адрес таблицы-приёмника Oracle и шаблон её создания — без драйвера:
модуль читает фасад инструмента на хосте."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from boba.db.oracle.query import OraIdentifier
from boba.toolkit.transfer import TemplateVar, TemplateVars, TransferError

__all__ = ["OraTableRef"]


@dataclass(frozen=True)
class OraTableRef:
    """Таблица приёмника: схема и имя как их назвал вызывающий. Кусок
    запроса для OraQueryBuilder: render отдаёт схему и имя через
    OraIdentifier — простое имя как есть (регистр решает сервер), иное в
    кавычках."""

    schema: str
    name: str

    CREATE_TABLE: ClassVar[str] = "create table {schema_name}.{table_name} ({columns})"
    """Шаблон без особенностей таблицы: дефолт фасада ora_stream_in."""
    TEMPLATE_VARS: ClassVar[TemplateVars] = TemplateVars(
        offered=(TemplateVar.SCHEMA_NAME, TemplateVar.TABLE_NAME, TemplateVar.COLUMNS)
    )
    """Переменные, которые приёмник Oracle подставляет в шаблон."""

    def __post_init__(self) -> None:
        if not self.schema.strip():
            raise TransferError("oracle table: expected a non-empty schema name")

        if not self.name.strip():
            raise TransferError("oracle table: expected a non-empty table name")

    def text(self) -> str:
        return f"{self.schema}.{self.name}"

    def render(self) -> str:
        schema = OraIdentifier(self.schema).render()
        name = OraIdentifier(self.name).render()

        return f"{schema}.{name}"
