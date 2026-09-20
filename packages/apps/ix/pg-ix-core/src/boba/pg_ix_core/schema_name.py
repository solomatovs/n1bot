"""Схема хранения графа: имя приходит из конфига и подставляется в запросы пакета.

В sql-файлах схема стоит плейсхолдером `{schema}` (`{schema}.node`), имя берётся
из секции конфига, а квотирует его psycopg (`sql.Identifier`), поэтому в текст
запроса не попадает ничего, кроме правильно закавыченного идентификатора.

Ошибки:
SchemaNameError — в тексте запроса остался неизвестный плейсхолдер.
"""

from __future__ import annotations

import re
from typing import ClassVar

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
    """Подстановка схемы в запросы пакета: `{schema}` -> закавыченное имя."""

    PLACEHOLDER: ClassVar[str] = "{schema}"
    LEFTOVER: ClassVar[re.Pattern[str]] = re.compile(r"\{[a-z_]+\}")

    @classmethod
    def render(cls, text: str, db_schema: str) -> bytes:
        """Текст запроса под схему; отдаёт bytes, потому что тип Query psycopg
        требует литерала, а текст пришёл из файла пакета."""
        quoted = sql.Identifier(db_schema).as_string()
        rendered = text.replace(cls.PLACEHOLDER, quoted)

        leftover = cls.LEFTOVER.search(rendered)
        if leftover is not None:
            msg = (
                f"schema render: unknown placeholder {leftover.group(0)} in the query; "
                f"only {cls.PLACEHOLDER} is substituted"
            )
            raise SchemaNameError(msg)

        return rendered.encode("utf-8")

    @classmethod
    def exists(cls, conn: psycopg.Connection, db_schema: str, table: str) -> bool:
        """Есть ли таблица в схеме графа: по ней пакет понимает, накачено ли ядро."""
        row = conn.execute(
            "select to_regclass(quote_ident(%s) || '.' || quote_ident(%s)) is not null",
            (db_schema, table),
        ).fetchone()

        return bool(row is not None and row[0])
