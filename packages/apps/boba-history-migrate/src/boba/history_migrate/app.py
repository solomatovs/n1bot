"""Вход скрипта миграции: файл настроек, прогон по тредам, отчёт.

Настройки — один toml-файл с правами 0600 (секреты только в нём): секция
[source] — подключение и схема checkpoints, [target] — подключение и схема
истории агента, [krb] — рабочий каталог Kerberos, если подключения
авторизуются билетом. Источник только читается; в целевой схеме записи
сессии появляются целиком или не появляются вовсе (append одной пачкой), и
тред с уже существующими записями пропускается — повторный запуск безопасен.

Ошибки:
MigrationError — файл настроек не читается, не приватен или не ложится в
    модель; сообщение называет файл и причину.
SystemExit — только из main(): MigrationError и наличие неразобранных тредов.
Отказ отдельного треда (источник, перевод, хранилище) в прогон не выходит:
    он ложится в отчёт строкой FAILED с текстом причины и в лог с трассой.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import stat
import sys
from collections.abc import Sequence
from enum import StrEnum
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from boba.agent.history import HistoryStore, SessionId
from boba.agent.ids import IdMint, SystemClock
from boba.agent.template import AgentTemplate
from boba.config import ConfigError, bind
from boba.config.section import compose_file
from boba.db.postgres import AsyncPostgresPool
from boba.db.postgres.connection import PostgresConfig
from boba.history.postgres import PostgresHistoryStore
from boba.history_migrate.source import CheckpointSource
from boba.history_migrate.translate import RecordTranslator
from boba.krb import KerberosWorkspace, KerberosWorkspaceConfig

logger = logging.getLogger(__name__)

__all__ = [
    "HistoryMigrateCli",
    "HistoryMigration",
    "MigrationArgs",
    "MigrationError",
    "MigrationReport",
    "MigrationSettings",
    "SettingsFile",
    "StoreLocation",
    "ThreadOutcome",
    "ThreadReport",
]

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


class MigrationError(Exception):
    """Настройки миграции не читаются."""


class StoreLocation(BaseModel):
    """Подключение к Postgres и схема в нём."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True, frozen=True)

    postgres: PostgresConfig
    db_schema: str = Field(alias="schema", min_length=1)


class MigrationSettings(BaseModel):
    """Файл настроек: откуда читать checkpoints, куда писать историю агента."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    source: StoreLocation
    target: StoreLocation
    krb: KerberosWorkspaceConfig | None = Field(
        default=None,
        description="Рабочий каталог Kerberos; нужен подключениям по билету.",
    )


class SettingsFile:
    """Чтение файла настроек: проверка прав, разбор toml, модель."""

    PRIVATE_MASK: ClassVar[int] = stat.S_IRWXG | stat.S_IRWXO

    def __init__(self, path: Path) -> None:
        self._path = path

    def load(self) -> MigrationSettings:
        """Ошибки:
        MigrationError — файла нет, он доступен не только владельцу или не
            ложится в модель.
        """
        try:
            mode = self._path.stat().st_mode
        except OSError as exc:
            msg = f"settings file {self._path}: cannot be read: {exc}"
            raise MigrationError(msg) from exc

        if mode & self.PRIVATE_MASK:
            msg = (
                f"settings file {self._path}: expected a private file (mode 0600), "
                f"got mode {stat.S_IMODE(mode):04o}. It holds database secrets; "
                "run chmod 0600 on it."
            )
            raise MigrationError(msg)

        try:
            raw = compose_file(self._path)
            return bind(raw, path="", model=MigrationSettings)
        except (ConfigError, ValidationError) as exc:
            msg = (
                f"settings file {self._path}: expected sections [source] and "
                f"[target] with postgres and schema, got: {exc}"
            )
            raise MigrationError(msg) from exc


class ThreadOutcome(StrEnum):
    """Чем кончилась миграция треда."""

    MIGRATED = "migrated"
    PRESENT = "present"
    EMPTY = "empty"
    FAILED = "failed"


class ThreadReport(BaseModel):
    """Итог одного треда."""

    model_config = ConfigDict(frozen=True)

    thread_id: str
    outcome: ThreadOutcome
    records: int = 0
    skipped: int = 0
    notes: Sequence[str] = ()
    error: str = ""


class MigrationReport(BaseModel):
    """Итог прогона: по строке на тред; печать и JSON — из него."""

    threads: Sequence[ThreadReport] = ()

    def of_outcome(self, outcome: ThreadOutcome) -> Sequence[ThreadReport]:
        chosen: list[ThreadReport] = []
        for thread in self.threads:
            if thread.outcome is outcome:
                chosen.append(thread)

        return chosen

    def records(self) -> int:
        total = 0
        for thread in self.threads:
            total += thread.records

        return total

    def render(self) -> str:
        """Текст отчёта для оператора."""
        migrated = self.of_outcome(ThreadOutcome.MIGRATED)
        present = self.of_outcome(ThreadOutcome.PRESENT)
        empty = self.of_outcome(ThreadOutcome.EMPTY)
        failed = self.of_outcome(ThreadOutcome.FAILED)

        lines: list[str] = [
            f"threads: {len(self.threads)} total, {len(migrated)} migrated "
            f"({self.records()} records), {len(present)} already present, "
            f"{len(empty)} without a checkpoint, {len(failed)} failed"
        ]
        if failed:
            lines.append("failed threads:")
            for thread in failed:
                lines.append(f"  {thread.thread_id}: {thread.error}")

        noted = list(self._noted())
        if noted:
            lines.append("threads with notes:")
            for thread in noted:
                lines.append(
                    f"  {thread.thread_id} ({thread.skipped} skipped messages): "
                    + "; ".join(thread.notes)
                )

        return "\n".join(lines)

    def _noted(self):
        for thread in self.threads:
            if thread.notes:
                yield thread


class HistoryMigration:
    """Прогон по тредам: расшифровка источника → перевод → запись пачкой.

    Создаётся входом скрипта из читателя источника, хранилища истории и
    переводчика; тесты собирают его же на своих стендах.
    """

    def __init__(
        self,
        source: CheckpointSource,
        target: HistoryStore,
        translator: RecordTranslator,
    ) -> None:
        self._source = source
        self._target = target
        self._translator = translator

    async def run(self, thread_ids: Sequence[str]) -> MigrationReport:
        reports: list[ThreadReport] = []
        for thread_id in thread_ids:
            report = await self._thread(thread_id)
            logger.info(
                "thread %s: %s, %d records, %d skipped messages",
                thread_id,
                report.outcome.value,
                report.records,
                report.skipped,
            )
            reports.append(report)

        return MigrationReport(threads=reports)

    async def _thread(self, thread_id: str) -> ThreadReport:
        """Граница треда: любой отказ — строка FAILED отчёта, трасса в логе."""
        try:
            return await self._migrate(thread_id)
        except Exception as exc:
            logger.exception("thread %s: migration failed", thread_id)
            return ThreadReport(
                thread_id=thread_id,
                outcome=ThreadOutcome.FAILED,
                error=f"{type(exc).__name__}: {exc}",
            )

    async def _migrate(self, thread_id: str) -> ThreadReport:
        transcript = await self._source.transcript(thread_id)
        if transcript is None:
            return ThreadReport(thread_id=thread_id, outcome=ThreadOutcome.EMPTY)

        translation = self._translator.translate(transcript)
        session = translation.session
        if await self._present(session.id):
            return ThreadReport(
                thread_id=thread_id,
                outcome=ThreadOutcome.PRESENT,
                notes=["the target already holds records of this session"],
            )

        records = session.records()
        lease = await self._target.acquire(session.id)
        try:
            await self._target.append(lease, records)
        finally:
            await self._target.release(lease)

        return ThreadReport(
            thread_id=thread_id,
            outcome=ThreadOutcome.MIGRATED,
            records=len(records),
            skipped=translation.skipped,
            notes=translation.notes,
        )

    async def _present(self, session_id: SessionId) -> bool:
        async for _line in self._target.read(session_id):
            return True

        return False


class Installed:
    """Версия скрипта из метаданных дистрибутива: ею подписываются записи."""

    DISTRIBUTION: ClassVar[str] = "boba-history-migrate"

    def version(self) -> str:
        """Ошибки:
        MigrationError — дистрибутив не установлен с метаданными.
        """
        try:
            return version(self.DISTRIBUTION)
        except PackageNotFoundError as exc:
            msg = (
                f"The distribution '{self.DISTRIBUTION}' is not installed with its "
                "metadata, so the records cannot carry a version. Install the "
                "package (pip install -e packages/apps/boba-history-migrate)."
            )
            raise MigrationError(msg) from exc


class MigrationArgs(BaseModel):
    """Разобранная командная строка."""

    model_config = ConfigDict(frozen=True)

    settings: Path
    report: Path | None = None
    threads: Sequence[str] = ()


class HistoryMigrateCli:
    """Команда boba-history-migrate: аргументы → настройки → прогон → отчёт."""

    PROGRAM: ClassVar[str] = "boba-history-migrate"

    def parse(self, argv: Sequence[str]) -> MigrationArgs:
        parser = argparse.ArgumentParser(
            prog=self.PROGRAM,
            description=(
                "Move chat history from the langgraph checkpoints schema into the "
                "agent history table. The source is only read."
            ),
        )
        parser.add_argument(
            "--settings",
            required=True,
            type=Path,
            help="toml file with [source], [target] and optional [krb]; mode 0600",
        )
        parser.add_argument(
            "--report",
            type=Path,
            default=None,
            help="where to write the JSON report; the text report goes to stdout",
        )
        parser.add_argument(
            "--thread",
            action="append",
            default=[],
            dest="threads",
            help="migrate only this thread id; repeatable; default is every thread",
        )
        parsed = parser.parse_args(argv)

        return MigrationArgs(
            settings=parsed.settings, report=parsed.report, threads=parsed.threads
        )

    async def run(self, args: MigrationArgs) -> MigrationReport:
        """Ошибки:
        MigrationError — настройки не читаются или дистрибутив без версии.
        boba.db.postgres.errors.PostgresError — пул источника или цели не
            открывается.
        """
        settings = SettingsFile(args.settings).load()
        installed = Installed().version()
        if settings.krb is not None:
            KerberosWorkspace.configure(settings.krb.config, settings.krb.ccache_dir)

        source_pool = AsyncPostgresPool(
            settings.source.postgres.with_schema(settings.source.db_schema)
        )
        target_pool = AsyncPostgresPool(settings.target.postgres)
        await source_pool.open()
        try:
            await target_pool.open()
            try:
                report = await self._migrate(
                    settings, args, source_pool, target_pool, installed
                )
            finally:
                await target_pool.close()
        finally:
            await source_pool.close()

        sys.stdout.write(report.render() + "\n")
        if args.report is not None:
            args.report.write_text(report.model_dump_json(indent=2), encoding="utf-8")

        return report

    async def _migrate(
        self,
        settings: MigrationSettings,
        args: MigrationArgs,
        source_pool: AsyncPostgresPool,
        target_pool: AsyncPostgresPool,
        installed: str,
    ) -> MigrationReport:
        source = CheckpointSource(source_pool, settings.source.db_schema)
        target = PostgresHistoryStore(
            settings.target.postgres, settings.target.db_schema, target_pool
        )
        translator = RecordTranslator(
            IdMint(), AgentTemplate(), SystemClock(), installed
        )

        thread_ids = args.threads
        if not thread_ids:
            thread_ids = await source.thread_ids()

        logger.info(
            "migrating %d thread(s) from %s.%s into %s.%s",
            len(thread_ids),
            settings.source.postgres.dbname,
            settings.source.db_schema,
            settings.target.postgres.dbname,
            settings.target.db_schema,
        )

        return await HistoryMigration(source, target, translator).run(thread_ids)


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
    command = HistoryMigrateCli()
    try:
        report = await command.run(command.parse(sys.argv[1:]))
    except MigrationError as exc:
        raise SystemExit(str(exc)) from exc

    failed = report.of_outcome(ThreadOutcome.FAILED)
    if failed:
        msg = f"{len(failed)} thread(s) failed to migrate; see the report above"
        raise SystemExit(msg)


def cli() -> None:
    asyncio.run(main())


if __name__ == "__main__":
    asyncio.run(main())
