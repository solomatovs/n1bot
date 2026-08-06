"""Индексация Confluence из терминала на конфиге приложения.

Тот же прогон, что у tool'ов confluence_index_*: конфиг берётся из
BOBA_CONFIG_PATH, секции [tool.ingest] и [tool.ingest.sandbox], работа
целиком идёт в песочнице. Параметры парсера (OCR) и режим обхода задаются
аргументами — в конфиге они не дублируются.

Логи прогона едут в тот же журнал, что у приложения: секция logger конфига.
"""

from __future__ import annotations

import argparse
import logging
import logging.config
import sys
from typing import ClassVar

from omegaconf import DictConfig

from boba.chainlit.infra.entry import AppEntry
from boba.sandbox import SandboxCaller, SandboxToolConfig
from boba.settings import bind
from boba.tool.kb.confluence.ingest_base import ConfluenceIngestConfig
from boba.tool.kb.confluence.ingest_caller import ConfluenceIngestCaller
from boba.toolkit.launcher import LauncherFactory, ToolLauncher

__all__ = ["ConfluenceIngestCli"]

logger = logging.getLogger("boba.chainlit.cli.ingest")


class ConfluenceIngestCli:
    """Запуск ingest-прогона: аргументы -> конфиг -> payload в песочнице."""

    TOOL: ClassVar[str] = "ingest"
    """Метка инструмента: с ней прогон виден в логах песочницы."""

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
        caller = ConfluenceIngestCaller(cls.TOOL, cls.launchers(raw))

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

        stats = caller.ingest(
            cfg=cfg,
            mode=args.mode,
            prune_missing=args.prune_missing,
            force_update=args.force_update,
            page_ids=cls.items(args) if args.mode == "pages" else (),
            cql=args.target if args.mode == "cql" else "",
            space_keys=cls.items(args) if args.mode == "spaces" else (),
        )
        logger.info("ingest done: %s", stats)
        return 0

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
    def config(
        cls, raw: DictConfig, args: argparse.Namespace
    ) -> ConfluenceIngestConfig:
        """Секция [tool.ingest] плюс параметры парсера из аргументов."""
        cfg = bind(raw, cls.SECTION, ConfluenceIngestConfig)
        return cfg.model_copy(
            update={
                "ocr_enabled": args.ocr_enabled,
                "num_workers": args.num_workers,
                "ocr_language": args.ocr_language,
            }
        )

    @classmethod
    def launchers(cls, raw: DictConfig) -> LauncherFactory:
        """Фабрика исполнителей на профиле [tool.ingest.sandbox]."""
        sandbox = bind(raw, f"{cls.SECTION}.sandbox", SandboxToolConfig)
        profile = sandbox.effective()

        def launcher(tool: str) -> ToolLauncher:
            # вне chainlit-сессии подстановок {user_id}/{thread_id} нет
            return SandboxCaller(tool, profile, dict)

        return launcher

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
