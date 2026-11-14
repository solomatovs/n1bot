"""Вход единого сервиса boba-agent и доступ к поставляемым SQL-файлам."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from importlib.resources import files
from pathlib import Path

from boba.agent import Installed
from boba.agent.queries import Query
from boba.agent.settings import ServeConfig
from boba.db.postgres.migration_options import MigrationArguments


async def main(argv=None) -> None:
    parser = argparse.ArgumentParser(
        prog="boba", description="PostgreSQL agent with ACP over HTTP/WebSocket"
    )
    parser.add_argument("--version", action="version", version=Installed().version())
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("serve", "health", "migrate"):
        command = commands.add_parser(name)
        command.add_argument("--config", type=Path, action="append", required=True)
        if name == "migrate":
            MigrationArguments().add(command)
    commands.add_parser("settings-schema")
    commands.add_parser("self-check")
    sql = commands.add_parser(
        "sql", help="Print a packaged SQL file for administration"
    )
    sql.add_argument("name")
    args = parser.parse_args(argv)
    if args.command == "sql":
        if args.name not in {item.value for item in Query}:
            parser.error("Unknown SQL file")
        sys.stdout.write(
            files("boba.agent").joinpath("sql", args.name + ".sql").read_text()
        )
    elif args.command == "self-check":
        for query in Query:
            query.text()
        sys.stdout.write("boba-agent SQL resources: OK\n")
    elif args.command == "settings-schema":
        sys.stdout.write(
            json.dumps(ServeConfig.model_json_schema(), ensure_ascii=False, indent=2)
            + "\n"
        )
    else:
        from boba.agent.serve import ServeEntry, prepare_migrations  # noqa: PLC0415

        if args.command == "migrate":
            from boba.config import ConfigSchema  # noqa: PLC0415
            from boba.db.postgres.migration_cli import (
                ApplicationMigrations,  # noqa: PLC0415
            )

            migrations = ApplicationMigrations(
                schema=ConfigSchema(ServeConfig, values=("auth",)),
                prepare=prepare_migrations,
            )
            await migrations.run(
                args.config, "boba.agent", options=MigrationArguments().read(args)
            )
        else:
            entry = ServeEntry(args.config)
            if args.command == "health":
                entry.health()
            else:
                await entry.run()


def cli() -> None:
    asyncio.run(main())


if __name__ == "__main__":
    cli()
