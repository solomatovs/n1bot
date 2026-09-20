"""Схема хранения графа: имя приходит из конфига и подставляется в запросы пакета.

В sql-файлах схема стоит плейсхолдером `{schema}` (`{schema}.node`) в нотации
`psycopg.sql.SQL.format`: имя берётся из секции конфига и подставляется как
`sql.Identifier`, поэтому в запрос попадает только закавыченный идентификатор.
Литеральная скобка в файле пишется удвоенной, как у `str.format`.

Ошибки:
SchemaNameError — текст запроса не собрался под схему: неизвестный плейсхолдер
    или непарная скобка.
"""

from __future__ import annotations

from typing import ClassVar, LiteralString, cast

import psycopg
from psycopg import sql
from pydantic import BaseModel, ConfigDict, Field

__all__ = ["SchemaName", "SchemaNameError", "StorageSchema"]


class SchemaNameError(Exception):
    """Текст запроса не удалось собрать под схему."""


class StorageSchema(BaseModel):
    """Имя схемы графа в базе; общее поле всех секций конфига приложений ix."""

    model_config = ConfigDict(extra="ignore")

    DEFAULT: ClassVar[str] = "ix"

    db_schema: str = Field(min_length=1, default=DEFAULT)


class SchemaName:
    """Подстановка схемы в запросы пакета через `sql.SQL.format`."""

    @classmethod
    def render(cls, text: str, db_schema: str, **parts: sql.Composable) -> sql.Composed:
        """Запрос под схему; остальные плейсхолдеры файла (`{sources}`, `{weights}`)
        заполняются переданными частями. cast до LiteralString безопасен: источник
        текста — файл пакета или объявление из базы, не пользовательский ввод."""
        template = sql.SQL(cast(LiteralString, text))

        try:
            return template.format(schema=sql.Identifier(db_schema), **parts)
        except (KeyError, ValueError, IndexError) as exc:
            known = ", ".join(["schema", *sorted(parts)])
            msg = (
                f"schema render for {db_schema!r}: query template expects only "
                f"placeholders {known}, got {exc!r}"
            )
            raise SchemaNameError(msg) from exc

    @classmethod
    def exists(cls, conn: psycopg.Connection, db_schema: str, table: str) -> bool:
        """Есть ли таблица в схеме графа: по ней пакет понимает, накачено ли ядро."""
        row = conn.execute(
            "select to_regclass(quote_ident(%s) || '.' || quote_ident(%s)) is not null",
            (db_schema, table),
        ).fetchone()

        return bool(row is not None and row[0])
