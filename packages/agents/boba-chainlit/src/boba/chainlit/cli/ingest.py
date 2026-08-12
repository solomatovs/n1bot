"""Индексация Confluence из терминала на конфиге приложения.

Тот же прогон, что у tool'ов confluence_index_*: конфиг берётся из
BOBA_CONFIG_PATH, секции [tool.ingest] и [tool.ingest.sandbox], работа
целиком идёт в песочнице. Параметры парсера (OCR) и режим обхода задаются
аргументами — в конфиге они не дублируются.

Реестр стадий прогона собирается из узлов секции [tool.ingest]: чужих узлов в
нём нет, поэтому вне сессии права на них не спрашиваются.

Вне сессии чата адрес вызова ставит сам прогон: пользователь `cli`, тред —
метка запуска, call_id — порядковый номер вызова. Журнал вывода обязателен и
здесь: том берётся из секции [stream_journal].

Логи прогона едут в тот же журнал, что у приложения: секция logger конфига.
"""

from __future__ import annotations

import argparse
import logging
import logging.config
import os
import sys
import time
from collections.abc import Mapping
from typing import TYPE_CHECKING, ClassVar

from omegaconf import DictConfig

from boba.chainlit.infra.entry import AppEntry
from boba.sandbox import SandboxCaller, SandboxToolConfig
from boba.sandbox.journal import DirVault, StreamJournal
from boba.sandbox.runner import ToolCallContext
from boba.sandbox.workflow import StageDef, StageRegistry
from boba.settings import bind
from boba.tool.kb.confluence.ingest_base import ConfluenceIngestConfig
from boba.tool.kb.confluence.ingest_caller import ConfluenceIngestCaller
from boba.tool.kb.confluence.ingest_protocol import IngestMode
from boba.tool.kb.confluence.ingest_stages import ConfluenceIngestStages
from boba.toolkit.channels import ChannelSink, StreamKey
from boba.toolkit.launcher import (
    ChannelHead,
    LauncherFactory,
    ToolLauncher,
)
from boba.toolkit.workflow import WorkflowOutcome, WorkflowSpec

if TYPE_CHECKING:
    # конфиг приложения тянет chainlit: на импорте модуля его быть не должно
    from boba.chainlit.infra.config import StreamJournalConfig

__all__ = ["CliNodeAccess", "CliRunLauncher", "ConfluenceIngestCli"]


class CliNodeAccess:
    """Права вне сессии: реестр собран под сам прогон, чужих узлов в нём нет."""

    def __call__(self, tool: str, /) -> bool:
        return True


class CliRunLauncher(ToolLauncher):
    """Адрес вызова для прогона вне сессии: нумерует вызовы прогона.

    Сессии чата нет, контекст ставить некому — его ставит запуск: журнал
    каждого вызова уезжает под своим номером в тред прогона.
    """

    USER: ClassVar[str] = "cli"

    def __init__(self, inner: ToolLauncher, tool: str, thread_id: str) -> None:
        self._inner = inner
        self._tool = tool
        self._thread_id = thread_id
        self._calls = 0

    @classmethod
    def run_mark(cls, tool: str) -> str:
        """Метка запуска вместо треда: время старта и pid — прогоны не смешиваются."""
        started = time.strftime("%Y%m%d-%H%M%S")

        return f"{tool}-{started}-{os.getpid()}"

    def call(
        self,
        spec: WorkflowSpec,
        sinks: Mapping[str, ChannelSink] | None = None,
    ) -> WorkflowOutcome:
        self._calls += 1
        context = ToolCallContext(
            user_id=self.USER,
            thread_id=self._thread_id,
            call_id=f"call{self._calls}",
            tool=self._tool,
        )

        token = ToolCallContext.set(context)
        try:
            return self._inner.call(spec, sinks)
        finally:
            ToolCallContext.reset(token)

    def head(self, key: StreamKey, max_bytes: int) -> ChannelHead:
        return self._inner.head(key, max_bytes)


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

        cfg = cls.config(raw)
        caller = ConfluenceIngestCaller(
            cls.TOOL, cls.launchers(raw, cfg, app.stream_journal)
        )

        logger.info(
            "ingest start: mode=%s target=%s ocr=%s workers=%d lang=%s "
            "force_update=%s prune_missing=%s collection=%s",
            args.mode,
            args.target,
            args.ocr_enabled,
            args.num_workers,
            args.ocr_language,
            args.force_update,
            args.prune_missing,
            cfg.collection,
        )

        page_ids: tuple[str, ...] = ()
        if args.mode == "pages":
            page_ids = cls.items(args)
        cql = ""
        if args.mode == "cql":
            cql = args.target
        space_keys: tuple[str, ...] = ()
        if args.mode == "spaces":
            space_keys = cls.items(args)

        stats = caller.ingest(
            mode=IngestMode(args.mode),
            prune_missing=args.prune_missing,
            force_update=args.force_update,
            ocr_enabled=args.ocr_enabled,
            num_workers=args.num_workers,
            ocr_language=args.ocr_language,
            page_ids=page_ids,
            cql=cql,
            space_keys=space_keys,
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
    def config(cls, raw: DictConfig) -> ConfluenceIngestConfig:
        """Секция [tool.ingest]; параметры парсера едут аргументами прогона."""
        return bind(raw, cls.SECTION, ConfluenceIngestConfig)

    @classmethod
    def launchers(
        cls,
        raw: DictConfig,
        cfg: ConfluenceIngestConfig,
        journal_cfg: StreamJournalConfig,
    ) -> LauncherFactory:
        """Фабрика исполнителей на узлах [tool.ingest] и её профиле песочницы."""
        sandbox = bind(raw, f"{cls.SECTION}.sandbox", SandboxToolConfig)
        profile = sandbox.effective()

        defs: dict[str, StageDef] = {}
        for name, node in ConfluenceIngestStages.of(cfg).items():
            defs[name] = StageDef.of(node, profile)

        vault = DirVault(journal_cfg.dir)
        vault.ensure_root()

        journal = StreamJournal(vault, journal_cfg.reserve_bytes)

        # вне chainlit-сессии подстановок {user_id}/{thread_id} нет
        caller = SandboxCaller(StageRegistry(defs), CliNodeAccess(), dict, journal)
        runs = CliRunLauncher(caller, cls.TOOL, CliRunLauncher.run_mark(cls.TOOL))

        def launcher(tool: str) -> ToolLauncher:
            return runs

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
