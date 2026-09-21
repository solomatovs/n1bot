"""Цикл описателя: очередь объектов из ix, описание моделью, запись в
{schema}.llm_description.

Описатель не знает ни поверхностей, ни того, что говорить модели. Материал объекта
даёт объявление аспекта класса describer_input, а роль модели и шаблон запроса — строка
владельца в {schema}.surface_prompt. Описываются только пары «поверхность, аспект», у
которых есть и объявление, и промпт: пара без промпта пропускается, и это видно в логе.

Один цикл: 05_declare.sql объявляет аспект llm_description для поверхностей с входом,
источник входа собирается из объявлений и подставляется вместо `{sources}`; дальше
10_queue.sql пачкой, на каждый объект описание (Description: один вызов или свёртка
длинного материала), 20_write.sql, 90_unlock.sql, и так до пустой очереди; в конце
30_prune.sql.

Отпечаток считается на каждую пару: модель, бюджет входа, промпт пары и шаблоны
пакета. Поэтому правка промпта одной поверхности переводит в очередь только её
объекты, а не все описания сразу.

Ошибки:
DescriberWorkerError — база или провайдер недоступны, ответ модели не по схеме, файлы
    пакета не найдены, промпт пары нарушает контракт.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import logging
from collections.abc import Mapping, Sequence
from enum import StrEnum
from pathlib import Path
from typing import Any, ClassVar

import psycopg
from psycopg import sql
from pydantic import BaseModel, ConfigDict, Field

from boba.config import ConfigError, bind_section
from boba.ix_core.aspects import AspectClass, AspectDeclarations, AspectSources
from boba.ix_core.database import IxDatabase, IxDatabaseError, IxPool
from boba.ix_core.prompts import SurfacePromptError, SurfacePrompts
from boba.ix_core.schema_name import SchemaName
from boba.ix_core.upgrade import SchemaUpgrade, SchemaUpgradeError
from boba.ix_llm_describer.describe import (
    DescribeError,
    Description,
    Generators,
    ModelConfig,
    PackPrompts,
)

logger = logging.getLogger("ix-llm-describer")


class DescriberWorkerError(Exception):
    """Ошибка цикла описателя."""


class SqlFile(StrEnum):
    DECLARE = "05_declare.sql"
    QUEUE = "10_queue.sql"
    WRITE = "20_write.sql"
    PRUNE = "30_prune.sql"
    UNLOCK = "90_unlock.sql"


class Part(StrEnum):
    """Плейсхолдеры файлов run/, которые заполняет воркер."""

    SOURCES = "sources"


class WorkerConfig(IxDatabase, ModelConfig):
    """Секция [ix.llm_describer]: база ix, модель с её бюджетом входа и размер пачки."""

    classes: Sequence[AspectClass]
    batch: int = Field(gt=0, default=8)


class QueueRow(BaseModel):
    node_id: int
    surface: str
    aspect: str
    text: str
    input_hash: str

    def pair(self) -> tuple[str, str]:
        return (self.surface, self.aspect)


class CycleReport(BaseModel):
    rounds: int
    written: int
    folded: int
    """Сколько объектов описано свёрткой, то есть не влезло в бюджет одним вызовом."""
    pruned: int


class PackageSql:
    """Файлы цикла из каталога run/ пакета; параметры именованные, в стиле psycopg,
    плейсхолдеры файла заполняются частями, собранными воркером при старте."""

    def __init__(
        self, package_dir: Path, db_schema: str, parts: Mapping[str, sql.Composable]
    ) -> None:
        self._dir = package_dir
        self._db_schema = db_schema
        self._parts = dict(parts)

    def load(self, name: SqlFile) -> sql.Composed:
        text = (self._dir / name).read_text(encoding="utf-8")

        return SchemaName.render(text, self._db_schema, **self._parts)


class Binding(BaseModel):
    """Что воркер собрал при старте цикла: файлы под источник входа, промпты пар и
    отпечаток каждой пары."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    sql_files: PackageSql
    prompts: SurfacePrompts
    hashes: Mapping[tuple[str, str], str]

    def surfaces(self) -> list[str]:
        found: list[str] = []
        for surface, _ in self.hashes:
            found.append(surface)

        return found

    def aspects(self) -> list[str]:
        found: list[str] = []
        for _, aspect in self.hashes:
            found.append(aspect)

        return found

    def fingerprints(self) -> list[str]:
        return list(self.hashes.values())


class DescriberWorker:
    """Цикл описателя: одна сессия к ix, описание объекта промптом его поверхности."""

    def __init__(
        self,
        cfg: WorkerConfig,
        package_dir: Path,
        pack: PackPrompts,
        describer: Description,
    ) -> None:
        self._cfg = cfg
        self._dir = package_dir
        self._pack = pack
        self._describer = describer

    async def run(self) -> CycleReport:
        try:
            async with IxPool.session(self._cfg) as conn:
                binding = await self._bind(conn)

                try:
                    rounds, written, folded = await self._rounds(conn, binding)
                finally:
                    await self._unlock(conn, binding)

                pruned = await self._prune(conn, binding)

                return CycleReport(
                    rounds=rounds, written=written, folded=folded, pruned=pruned
                )
        except IxDatabaseError as exc:
            raise DescriberWorkerError(str(exc)) from exc
        except SurfacePromptError as exc:
            raise DescriberWorkerError(f"describe prompts: {exc}") from exc
        except DescribeError as exc:
            raise DescriberWorkerError(str(exc)) from exc
        except psycopg.Error as exc:
            msg = f"ix database {self._cfg.postgres.where()}: {exc}"
            raise DescriberWorkerError(msg) from exc

    async def _bind(self, conn: psycopg.AsyncConnection[Any]) -> Binding:
        """Объявить llm_description, собрать источник входа и промпты пар."""
        declare = PackageSql(self._dir, self._cfg.db_schema, {})
        await conn.execute(declare.load(SqlFile.DECLARE))

        declarations = await AspectDeclarations.of_classes(
            conn, self._cfg.db_schema, self._cfg.classes
        )
        prompts = await SurfacePrompts.load(conn, self._cfg.db_schema)

        hashes: dict[tuple[str, str], str] = {}
        skipped: list[str] = []
        for declaration in declarations:
            pair = (declaration.surface, declaration.aspect)
            if pair not in prompts.pairs():
                skipped.append(f"{pair[0]}/{pair[1]}")
                continue

            prompt = prompts.of(pair[0], pair[1])
            prompt.check()
            hashes[pair] = self._fingerprint(prompt.system_prompt, prompt.user_template)

        names: list[str] = []
        for surface, aspect in hashes:
            names.append(f"{surface}/{aspect}")

        logger.info("describing pairs: %s", ", ".join(names))

        if skipped:
            logger.warning(
                "no prompt in %s.surface_prompt, skipped: %s",
                self._cfg.db_schema,
                ", ".join(skipped),
            )

        sql_files = PackageSql(
            self._dir,
            self._cfg.db_schema,
            {str(Part.SOURCES): AspectSources.union(declarations, self._cfg.db_schema)},
        )

        return Binding(sql_files=sql_files, prompts=prompts, hashes=hashes)

    def _fingerprint(self, system_prompt: str, user_template: str) -> str:
        """Отпечаток пары: модель, бюджет, промпт владельца и шаблоны пакета."""
        material = "\n".join(
            [
                self._cfg.label(),
                str(self._cfg.max_input_chars),
                system_prompt,
                user_template,
                self._pack.material(),
            ]
        )

        return hashlib.md5(material.encode("utf-8"), usedforsecurity=False).hexdigest()

    async def _rounds(
        self, conn: psycopg.AsyncConnection[Any], binding: Binding
    ) -> tuple[int, int, int]:
        rounds = 0
        written = 0
        folded = 0
        while True:
            rows = await self._queue(conn, binding)
            if not rows:
                break

            for row in rows:
                prompt = binding.prompts.of(row.surface, row.aspect)
                described = await self._describer.of(prompt, row.text)
                await self._write(conn, binding, row, described.text)
                written += 1
                if described.chunks > 1:
                    folded += 1
                    logger.info(
                        "node %d (%s): %d chars folded from %d chunks",
                        row.node_id,
                        row.surface,
                        len(row.text),
                        described.chunks,
                    )

            await self._unlock(conn, binding)
            rounds += 1
            logger.info("round %d: %d objects described", rounds, len(rows))

        return rounds, written, folded

    async def _queue(
        self, conn: psycopg.AsyncConnection[Any], binding: Binding
    ) -> list[QueueRow]:
        if not binding.hashes:
            return []

        params = {
            "batch": self._cfg.batch,
            "surfaces": binding.surfaces(),
            "aspects": binding.aspects(),
            "hashes": binding.fingerprints(),
        }
        cur = await conn.execute(binding.sql_files.load(SqlFile.QUEUE), params)

        rows: list[QueueRow] = []
        for node_id, surface, aspect, text, input_hash in await cur.fetchall():
            rows.append(
                QueueRow(
                    node_id=node_id,
                    surface=surface,
                    aspect=aspect,
                    text=text,
                    input_hash=input_hash,
                )
            )

        return rows

    async def _write(
        self,
        conn: psycopg.AsyncConnection[Any],
        binding: Binding,
        row: QueueRow,
        description: str,
    ) -> None:
        params = {
            "node_id": row.node_id,
            "surface": row.surface,
            "content": description,
            "input_hash": row.input_hash,
            "indexer_hash": binding.hashes[row.pair()],
        }
        await conn.execute(binding.sql_files.load(SqlFile.WRITE), params)

    async def _unlock(
        self, conn: psycopg.AsyncConnection[Any], binding: Binding
    ) -> None:
        await conn.execute(binding.sql_files.load(SqlFile.UNLOCK))

    async def _prune(self, conn: psycopg.AsyncConnection[Any], binding: Binding) -> int:
        cur = await conn.execute(binding.sql_files.load(SqlFile.PRUNE))
        record = await cur.fetchone()
        if record is None:
            raise DescriberWorkerError("prune: expected one summary row, got none")

        return int(record[1])


class Command(StrEnum):
    """Что делает запуск: накатить свою схему или отработать цикл."""

    UPGRADE = "upgrade"
    RUN = "run"


class Cli:
    """Команда и путь к конфигу; настройки берутся из секции [ix.llm_describer]."""

    SECTION: ClassVar[str] = "ix.llm_describer"

    @classmethod
    def parse(cls, argv: Sequence[str] | None = None) -> tuple[Command, Path]:
        parser = argparse.ArgumentParser(
            prog="boba-ix-llm-describer",
            description=(
                "Описатель объектов ix моделью: схема пакета и цикл описаний."
            ),
        )
        parser.add_argument(
            "command",
            type=Command,
            choices=list(Command),
            help=(
                "upgrade — накатить схему пакета в базу ix (идемпотентно, ядро "
                "должно быть уже накачено пакетом ix-core); run — рабочий цикл."
            ),
        )
        parser.add_argument(
            "--config",
            required=True,
            type=Path,
            help=(
                "Путь к файлу конфига приложения (toml). Все настройки, включая "
                "профиль подключения к базе ix, берутся из секции [ix.llm_describer]."
            ),
        )
        args = parser.parse_args(argv)

        return args.command, args.config


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")

    package_dir = Path(__file__).resolve().parent
    try:
        command, config_path = Cli.parse()

        if command is Command.UPGRADE:
            database = bind_section(config_path, Cli.SECTION, IxDatabase)
            upgrade = SchemaUpgrade(package_dir / "schema")
            report = asyncio.run(upgrade.run(database))
            logger.info("schema applied: %s", ", ".join(report.files))
            return

        cfg = bind_section(config_path, Cli.SECTION, WorkerConfig)
        pack = PackPrompts(package_dir / "prompt")
        describer = Description(Generators(cfg), pack, cfg.max_input_chars)
        worker = DescriberWorker(cfg, package_dir / "run", pack, describer)
        report = asyncio.run(worker.run())
        logger.info(
            "done: rounds=%d written=%d folded=%d pruned=%d",
            report.rounds,
            report.written,
            report.folded,
            report.pruned,
        )
    except (
        ConfigError,
        SchemaUpgradeError,
        DescribeError,
        DescriberWorkerError,
    ) as exc:
        raise SystemExit(str(exc)) from exc
