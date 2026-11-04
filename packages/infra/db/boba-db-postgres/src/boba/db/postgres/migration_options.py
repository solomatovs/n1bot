"""Аргументы выбора SQL-версии, базы и схемы.

Ошибки:
ValueError — имя версии, базы или схемы передано пустой строкой."""

from __future__ import annotations

import argparse
from dataclasses import dataclass


@dataclass(frozen=True)
class MigrationOptions:
    """Выбор SQL-версии приложения и места выполнения всех его планов."""

    version: str | None = None
    database: str | None = None
    schema: str | None = None

    def __post_init__(self) -> None:
        for value in (self.version, self.database, self.schema):
            if value is not None:
                if not value.strip():
                    raise ValueError(
                        "migration version, database and schema must not be empty"
                    )


class MigrationArguments:
    """Одинаковые флаги команды migrate у AID, Boba и MCP."""

    def add(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "--migration-version", help="application SQL directory; latest by default"
        )
        parser.add_argument(
            "--database",
            help="override the database for the application and all plugins",
        )
        parser.add_argument(
            "--schema", help="override the schema for the application and all plugins"
        )

    def read(self, arguments: argparse.Namespace) -> MigrationOptions:
        return MigrationOptions(
            arguments.migration_version,
            arguments.database,
            arguments.schema,
        )
