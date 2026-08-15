"""Индексация Confluence из терминала на конфиге приложения.

Тот же прогон, что у tool'ов confluence_index_*: конфиг берётся из
BOBA_CONFIG_PATH, секция [tool.ingest], тело функции вызывается напрямую —
как в тестах. Параметры парсера (OCR) и режим обхода задаются аргументами.

Логи прогона едут в тот же журнал, что у приложения: секция logger конфига.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import logging.config
import sys
from typing import ClassVar

from omegaconf import DictConfig

from boba.chainlit.infra.entry import AppEntry
from boba.settings import bind
from boba.tool.kb.confluence.ingest_tools import (
    IngestToolConfig,
    confluence_index_cql,
    confluence_index_pages,
    confluence_index_spaces,
)
from boba.toolkit.entry import ToolMain

__all__ = ["ConfluenceIngestCli"]

logger = logging.getLogger("boba.chainlit.cli.ingest")


class ConfluenceIngestCli:
    """Запуск ingest-прогона: аргументы -> конфиг -> прямой вызов функции."""

    SECTION: ClassVar[str] = "tool.ingest"

    @classmethod
    def main(cls, argv: list[str]) -> int:
        args = cls.parser().parse_args(argv)

        config_path = AppEntry.config_path()
        AppEntry.export_env(config_path)

        # импорт здесь: chainlit фиксирует пути из env на импорте своих модулей
        from boba.chainlit.infra import providers  # noqa: PLC0415
        from boba.chainlit.infra.log_context import UserLogContext  # noqa: PLC0415

        app = providers.get_app_config(config_path=config_path)
        # форматтер приложения ждёт поле user в каждой записи; вне сессии это "-"
        UserLogContext.install()
        logging.config.dictConfig(app.logger)
        raw = providers.get_raw_config()

        cfg = cls.config(raw, args)

        logger.info(
            "ingest start: mode=%s target=%s ocr=%s workers=%d lang=%s "
            "force_update=%s prune_missing=%s collection=%s",
            args.mode,
            args.target,
            cfg.ocr_enabled,
            cfg.num_workers,
            cfg.ocr_language,
            args.force_update,
            args.prune_missing,
            cfg.collection,
        )

        content = asyncio.run(cls.run(cfg, args))
        logger.info("ingest done: %s", content)
        return 0

    @classmethod
    async def run(cls, cfg: IngestToolConfig, args: argparse.Namespace) -> str:
        """Прямой вызов тела нужной функции; настройки парсера уже в cfg."""
        if args.mode == "pages":
            body = ToolMain.toolset(confluence_index_pages)[0].coroutine
            if body is None:
                raise RuntimeError("confluence_index_pages has no coroutine")

            content, _artifact = await body(
                page_ids=list(cls.items(args)),
                prune_missing=args.prune_missing,
                force_update=args.force_update,
                cfg=cfg,
            )
            return str(content)

        if args.mode == "cql":
            body = ToolMain.toolset(confluence_index_cql)[0].coroutine
            if body is None:
                raise RuntimeError("confluence_index_cql has no coroutine")

            content, _artifact = await body(
                cql=args.target,
                prune_missing=args.prune_missing,
                cfg=cfg,
            )
            return str(content)

        body = ToolMain.toolset(confluence_index_spaces)[0].coroutine
        if body is None:
            raise RuntimeError("confluence_index_spaces has no coroutine")

        content, _artifact = await body(
            space_keys=list(cls.items(args)),
            prune_missing=args.prune_missing,
            force_update=args.force_update,
            cfg=cfg,
        )
        return str(content)

    @classmethod
    def parser(cls) -> argparse.ArgumentParser:
        parser = argparse.ArgumentParser(
            prog="boba-ingest",
            description="Индексация Confluence в базу знаний.",
        )
        parser.add_argument(
            "--mode",
            required=True,
            choices=("spaces", "pages", "cql"),
            help="Способ обхода: spaces/pages — списки через запятую, cql — запрос.",
        )
        parser.add_argument(
            "--target",
            required=True,
            help='Space-ключи ("DQ,IPKD"), page_id ("983049,983136") или CQL.',
        )
        parser.add_argument(
            "--ocr-enabled",
            action="store_true",
            help="Распознавать текст на картинках и сканах.",
        )
        parser.add_argument(
            "--ocr-language",
            default="rus+eng",
            help="Язык OCR в формате Tesseract.",
        )
        parser.add_argument(
            "--num-workers",
            type=int,
            default=1,
            choices=range(1, 5),
            help="Параллелизм OCR, 1..4.",
        )
        parser.add_argument(
            "--force-update",
            action="store_true",
            help="Переиндексировать всё, минуя пропуск неизменившихся.",
        )
        parser.add_argument(
            "--prune-missing",
            action="store_true",
            help="Снести из коллекции всё, что не попало в текущий прогон.",
        )
        return parser

    @classmethod
    def config(cls, raw: DictConfig, args: argparse.Namespace) -> IngestToolConfig:
        """Секция [tool.ingest] плюс параметры парсера из аргументов."""
        cfg = bind(raw, cls.SECTION, IngestToolConfig)
        return cfg.with_parser(
            ocr_enabled=args.ocr_enabled,
            num_workers=args.num_workers,
            ocr_language=args.ocr_language,
        )

    @staticmethod
    def items(args: argparse.Namespace) -> tuple[str, ...]:
        """Список через запятую -> кортеж непустых значений."""
        values: list[str] = []
        for raw_item in args.target.split(","):
            item = raw_item.strip()
            if item:
                values.append(item)
        return tuple(values)


if __name__ == "__main__":
    sys.exit(ConfluenceIngestCli.main(sys.argv[1:]))
