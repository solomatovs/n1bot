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
from pathlib import Path
from typing import ClassVar

from boba.config import ConfigError, bind_section
from boba.ix_core.database import IxDatabase
from boba.ix_core.upgrade import SchemaUpgrade, SchemaUpgradeError

logger = logging.getLogger("ix-core")

SCHEMA_DIR = Path(__file__).resolve().parent / "schema"


class Cli:
    """Запуск с одним аргументом --config: секция [ix.core] в модель."""

    SECTION: ClassVar[str] = "ix.core"
    COMMAND: ClassVar[str] = "upgrade"

    @classmethod
    def parse(cls, argv: Sequence[str] | None = None) -> IxDatabase:
        parser = argparse.ArgumentParser(
            prog="boba-ix-core",
            description="Ядро графа ix: схема ix, surface, node, tree, edge.",
        )
        parser.add_argument(
            cls.COMMAND,
            choices=[cls.COMMAND],
            help="Накатить схему ядра в базу из конфига; команда идемпотентна.",
        )
        parser.add_argument(
            "--config",
            required=True,
            type=Path,
            help=(
                "Путь к файлу конфига приложения (toml). База берётся из секции "
                f"[{cls.SECTION}]."
            ),
        )
        args = parser.parse_args(argv)

        return bind_section(args.config, cls.SECTION, IxDatabase)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")

    try:
        cfg = Cli.parse()
        upgrade = SchemaUpgrade(SCHEMA_DIR, requires_core=False)
        report = asyncio.run(upgrade.run(cfg))
        logger.info("core schema applied: %s", ", ".join(report.files))
    except (ConfigError, SchemaUpgradeError) as exc:
        raise SystemExit(str(exc)) from exc


if __name__ == "__main__":
    main()
