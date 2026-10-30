"""Команда ядра ix: накат схемы core (ix.surface, ix.node, ix.tree, ix.edge).

Пакет ничего не исполняет в рантайме: он владеет только DDL ядра, на который
опираются скраперы, индексаторы и описатели. Ставится первым, дальше каждый
пакет накатывает свою схему сам.

Ошибки:
SchemaUpgradeError — базу не удалось обновить.
ConfigError — конфига нет, секции нет или её поля не сходятся с моделью.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from collections.abc import Sequence
from importlib.resources import files
from pathlib import Path

from boba.config import ConfigError
from boba.ix_core.database import IxConfigFile, IxDatabase
from boba.ix_core.scrape import PackageDir
from boba.ix_core.upgrade import SchemaUpgrade, SchemaUpgradeError

logger = logging.getLogger("ix-core")


def parse_args(argv: Sequence[str] | None = None) -> IxDatabase:
    """Запуск с одним аргументом --config: секция [ix.core] в модель."""
    section = "ix.core"
    command = "upgrade"
    parser = argparse.ArgumentParser(
        prog="boba-ix-core",
        description="Ядро графа ix: схема ix, surface, node, tree, edge.",
    )
    parser.add_argument(
        command,
        choices=[command],
        help="Накатить схему ядра в базу из конфига; команда идемпотентна.",
    )
    parser.add_argument(
        "--config",
        required=True,
        type=Path,
        help=(
            "Путь к файлу конфига приложения (toml). База берётся из секции "
            f"[{section}]."
        ),
    )
    args = parser.parse_args(argv)
    config = IxConfigFile(args.config)
    config.enter_kerberos()

    return config.section(section, IxDatabase)


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")

    try:
        cfg = parse_args()
        schema_dir = files("boba.ix_core") / PackageDir.SCHEMA
        upgrade = SchemaUpgrade(schema_dir, requires_core=False)
        report = await upgrade.run(cfg)
        logger.info("core schema applied: %s", ", ".join(report.files))
    except (ConfigError, SchemaUpgradeError) as exc:
        raise SystemExit(str(exc)) from exc


def cli() -> None:
    """Точка входа консольного скрипта: единственный asyncio.run на процесс."""
    asyncio.run(main())


if __name__ == "__main__":
    asyncio.run(main())
