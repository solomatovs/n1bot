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
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

import psycopg
from psycopg import sql
from pydantic import Field

from boba.config import ConfigError, bind_section
from boba.db.postgres import AsyncPostgresPool, PostgresError
from boba.db.postgres.query import PgQueryBuilder
from boba.ix_core.aspects import AspectClass
from boba.ix_core.database import IxDatabase, enter_kerberos
from boba.ix_core.prompts import SurfacePromptError
from boba.ix_core.registry import IxRegistry
from boba.ix_core.upgrade import SchemaUpgrade, SchemaUpgradeError
from boba.ix_llm_describer.describe import DescribeError, Description, PackPrompts
from boba.llm.providers import ChatModelConfig, LlmProviders, LlmProviderTypes
from boba.llm.schema import SchemaReply

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


class WorkerConfig(IxDatabase):
    """Секция [ix.llm_describer]: база ix, чат-модель, бюджет входа и размер пачки."""

    classes: Sequence[AspectClass]
    chat: ChatModelConfig
    max_input_chars: int = Field(gt=0)
    """Сколько знаков материала модель принимает за один вызов; длиннее — свёртка."""
    batch: int = Field(gt=0)

    def model_label(self) -> str:
        """Модель для отпечатка: её смена перегоняет описания заново."""
        return f"{self.chat.provider.kind}:{self.chat.model}"


@dataclass(frozen=True, kw_only=True)
class QueueRow:
    node_id: int
    surface: str
    aspect: str
    text: str
    input_hash: str

    def pair(self) -> tuple[str, str]:
        return (self.surface, self.aspect)


@dataclass(frozen=True, kw_only=True)
class CycleReport:
    rounds: int
    written: int
    folded: int
    """Сколько объектов описано свёрткой, то есть не влезло в бюджет одним вызовом."""
    pruned: int


@dataclass(frozen=True, kw_only=True)
class Binding:
    """Что воркер собрал при старте цикла: файлы под источник входа, промпты пар и
    отпечаток каждой пары."""

    names: Mapping[str, sql.Composable]
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
        self._registry = IxRegistry(cfg.db_schema)

    async def run(self) -> CycleReport:
        try:
            async with await AsyncPostgresPool.dedicated(self._cfg.postgres) as conn:
                binding = await self._prepare(conn)

                try:
                    rounds, written, folded = await self._rounds(conn, binding)
                finally:
                    await self._unlock(conn, binding)

                pruned = await self._prune(conn, binding)

                return CycleReport(
                    rounds=rounds, written=written, folded=folded, pruned=pruned
                )
        except PostgresError as exc:
            raise DescriberWorkerError(str(exc)) from exc
        except SurfacePromptError as exc:
            raise DescriberWorkerError(f"describe prompts: {exc}") from exc
        except DescribeError as exc:
            raise DescriberWorkerError(str(exc)) from exc
        except psycopg.Error as exc:
            msg = f"ix database {self._cfg.postgres.where()}: {exc}"
            raise DescriberWorkerError(msg) from exc

    async def _prepare(self, conn: psycopg.AsyncConnection[Any]) -> Binding:
        """Объявить llm_description, собрать источник входа и промпты пар."""
        query = (
            PgQueryBuilder(schema=sql.Identifier(self._cfg.db_schema))
            .read(self._dir / SqlFile.DECLARE)
            .build()
        )
        await conn.execute(query.text, query.params)

        declarations = await self._registry.read_declarations(conn, self._cfg.classes)
        await self._registry.read_prompts(conn)

        hashes: dict[tuple[str, str], str] = {}
        skipped: list[str] = []
        for declaration in declarations:
            pair = (declaration.surface, declaration.aspect)
            if pair not in self._registry.prompt_pairs():
                skipped.append(f"{pair[0]}/{pair[1]}")
                continue

            prompt = self._registry.prompt_of(pair[0], pair[1])
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

        sql_names: dict[str, sql.Composable] = {
            "schema": sql.Identifier(self._cfg.db_schema),
            Part.SOURCES: self._registry.union_sources(declarations),
        }

        return Binding(names=sql_names, hashes=hashes)

    def _fingerprint(self, system_prompt: str, user_template: str) -> str:
        """Отпечаток пары: модель, бюджет, промпт владельца и шаблоны пакета."""
        material = "\n".join(
            [
                self._cfg.model_label(),
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
                prompt = self._registry.prompt_of(row.surface, row.aspect)
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
        query = (
            PgQueryBuilder(**binding.names)
            .read(self._dir / SqlFile.QUEUE, **params)
            .build()
        )
        cur = await conn.execute(query.text, query.params)

        rows: list[QueueRow] = []
        async for node_id, surface, aspect, text, input_hash in cur:
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
        query = (
            PgQueryBuilder(**binding.names)
            .read(self._dir / SqlFile.WRITE, **params)
            .build()
        )
        await conn.execute(query.text, query.params)

    async def _unlock(
        self, conn: psycopg.AsyncConnection[Any], binding: Binding
    ) -> None:
        query = PgQueryBuilder(**binding.names).read(self._dir / SqlFile.UNLOCK).build()
        await conn.execute(query.text, query.params)

    async def _prune(self, conn: psycopg.AsyncConnection[Any], binding: Binding) -> int:
        query = PgQueryBuilder(**binding.names).read(self._dir / SqlFile.PRUNE).build()
        cur = await conn.execute(query.text, query.params)
        record = await cur.fetchone()
        if record is None:
            raise DescriberWorkerError("prune: expected one summary row, got none")

        return int(record[1])


class Command(StrEnum):
    """Что делает запуск: накатить свою схему или отработать цикл."""

    UPGRADE = "upgrade"
    RUN = "run"


def parse_args(argv: Sequence[str] | None = None) -> tuple[Command, Path]:
    """Команда и путь к конфигу; настройки берутся из секции [ix.llm_describer]."""
    parser = argparse.ArgumentParser(
        prog="boba-ix-llm-describer",
        description=("Описатель объектов ix моделью: схема пакета и цикл описаний."),
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


async def main() -> None:
    section = "ix.llm_describer"
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")

    package_dir = Path(__file__).resolve().parent
    try:
        command, config_path = parse_args()
        enter_kerberos(config_path)

        if command is Command.UPGRADE:
            database = bind_section(config_path, section, IxDatabase)
            upgrade = SchemaUpgrade(package_dir / "schema")
            report = await upgrade.run(database)
            logger.info("schema applied: %s", ", ".join(report.files))
            return

        cfg = bind_section(config_path, section, WorkerConfig)
        pack = PackPrompts(package_dir / "prompt")
        providers = LlmProviders(LlmProviderTypes.installed())
        try:
            reply = SchemaReply(providers.chat(cfg.chat), cfg.chat.sampling)
            describer = Description(reply, pack, cfg.max_input_chars)
            worker = DescriberWorker(cfg, package_dir / "run", pack, describer)
            report = await worker.run()
        finally:
            await providers.aclose()
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


def cli() -> None:
    """Точка входа консольного скрипта: единственный asyncio.run на процесс."""
    asyncio.run(main())


if __name__ == "__main__":
    asyncio.run(main())
