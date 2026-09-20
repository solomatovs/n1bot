"""
Воркер описателя: очередь объектов из ix, описание моделью через порт
StructuredGenerator
проекта, запись в ix.pg_llm_description.

Один цикл: 10_queue.sql пачкой, на каждый объект generate(user, schema) провайдера
boba.llm.generation с системным промптом, шаблоном входа и json-схемой из prompt/,
20_write.sql, 90_unlock.sql, и так до пустой очереди; в конце 30_prune.sql.
indexer_hash это md5 модели и трёх файлов prompt/: смена любого переводит всё в очередь.

Ошибки:
DescriberWorkerError — база или провайдер недоступны, ответ модели не по схеме, файлы
    пакета не найдены.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
from collections.abc import Sequence
from enum import StrEnum
from pathlib import Path
from typing import ClassVar

import psycopg
from psycopg import sql
from pydantic import BaseModel, Field, ValidationError

from boba.chat.generation import (
    GenerationError,
    LocalGeneration,
    OpenAiGeneration,
    SchemaSpec,
    StructuredGenerator,
)
from boba.chat.http import HttpConfig
from boba.config import ConfigError, bind_section
from boba.llm.generation import GeneratorFactory
from boba.llm.http import LlmHttp
from boba.llm.local import OnnxChatRuntime
from boba.pg_ix_core.schema_name import SchemaName, StorageSchema
from boba.pg_ix_core.upgrade import (
    SchemaUpgrade,
    SchemaUpgradeError,
    UpgradeConfig,
)

logger = logging.getLogger("pg-llm-describer")


class DescriberWorkerError(Exception):
    """Ошибка цикла описателя."""


class SqlFile(StrEnum):
    QUEUE = "10_queue.sql"
    WRITE = "20_write.sql"
    PRUNE = "30_prune.sql"
    UNLOCK = "90_unlock.sql"


class PromptFile(StrEnum):
    SYSTEM = "system.md"
    USER = "user.md"
    SCHEMA = "schema.json"


class Provider(StrEnum):
    OPENAI = "openai"
    LOCAL = "local"


class WorkerConfig(StorageSchema):
    dsn: str
    provider: Provider
    model: str = ""
    base_url: str = ""
    api_key: str = ""
    model_dir: str = ""
    max_tokens: int = Field(gt=0, default=1024)
    temperature: float = Field(ge=0, default=0.2)
    tool_choice: str = "auto"
    batch: int = Field(gt=0, default=8)
    lock_timeout: str = "2s"
    statement_timeout: str = "60s"


class QueueRow(BaseModel):
    node_id: int
    surface: str
    text: str
    input_hash: str


class Reply(BaseModel):
    description: str = Field(min_length=1)


class CycleReport(BaseModel):
    rounds: int
    written: int
    pruned: int


class Prompts:
    """
    Три файла prompt/: системный промпт, шаблон входа с плейсхолдером {input},
    json-схема
    ответа. Их md5 вместе с именем модели даёт indexer_hash."""

    PLACEHOLDER = "{input}"

    def __init__(self, prompt_dir: Path) -> None:
        self.system = (
            (prompt_dir / PromptFile.SYSTEM).read_text(encoding="utf-8").strip()
        )
        self.user_template = (
            (prompt_dir / PromptFile.USER).read_text(encoding="utf-8").strip()
        )
        raw = json.loads((prompt_dir / PromptFile.SCHEMA).read_text(encoding="utf-8"))
        self.schema = SchemaSpec.model_validate(raw)
        if self.PLACEHOLDER not in self.user_template:
            raise DescriberWorkerError(
                f"{prompt_dir / PromptFile.USER}: expected placeholder "
                "{self.PLACEHOLDER}"
            )

    def user(self, text: str) -> str:
        return self.user_template.replace(self.PLACEHOLDER, text)

    def fingerprint(self, model: str) -> str:
        material = "\n".join(
            [
                model,
                self.system,
                self.user_template,
                json.dumps(self.schema.body, sort_keys=True, ensure_ascii=False),
            ]
        )
        return hashlib.md5(material.encode("utf-8"), usedforsecurity=False).hexdigest()


class PackageSql:
    """Файлы цикла из каталога run/ пакета; параметры именованные, в стиле psycopg."""

    def __init__(self, package_dir: Path, db_schema: str) -> None:
        self._dir = package_dir
        self._db_schema = db_schema

    def load(self, name: SqlFile) -> bytes:
        text = (self._dir / name).read_text(encoding="utf-8")
        return SchemaName.render(text, self._db_schema)


class Generators:
    """
    Сборка генератора проекта по флагам воркера: openai-совместимый endpoint или
    локальная
    onnx-модель. Системный промпт живёт в конфиге генератора, поэтому он собирается
    здесь."""

    @classmethod
    def build(cls, cfg: WorkerConfig, prompts: Prompts) -> StructuredGenerator:
        if cfg.provider is Provider.LOCAL:
            if not cfg.model_dir:
                raise DescriberWorkerError("provider local: expected --model-dir")
            local = LocalGeneration(
                kind="local",
                system_prompt=prompts.system,
                max_tokens=cfg.max_tokens,
                model_dir=cfg.model_dir,
                reply_prefix="",
            )
            return GeneratorFactory.build(
                local, client=None, runtime=OnnxChatRuntime(cfg.model_dir)
            )
        if not cfg.base_url or not cfg.model:
            raise DescriberWorkerError(
                "provider openai: expected --base-url and --model"
            )
        remote = OpenAiGeneration(
            kind="openai",
            system_prompt=prompts.system,
            http=HttpConfig(),
            base_url=cfg.base_url,
            api_key=cfg.api_key,
            model=cfg.model,
            sampling={
                "temperature": cfg.temperature,
                "max_tokens": cfg.max_tokens,
                "tool_choice": cfg.tool_choice,
            },
        )
        return GeneratorFactory.build(
            remote, client=LlmHttp.client(remote.http), runtime=None
        )

    @staticmethod
    def label(cfg: WorkerConfig) -> str:
        if cfg.provider is Provider.LOCAL:
            return f"local:{cfg.model_dir}"
        return f"openai:{cfg.model}"


class DescriberWorker:
    """Цикл описателя: одна сессия к ix, один генератор."""

    def __init__(
        self,
        cfg: WorkerConfig,
        sql_files: PackageSql,
        prompts: Prompts,
        generator: StructuredGenerator,
    ) -> None:
        self._cfg = cfg
        self._sql = sql_files
        self._prompts = prompts
        self._generator = generator
        self._indexer_hash = prompts.fingerprint(Generators.label(cfg))

    async def run(self) -> CycleReport:
        try:
            with psycopg.connect(
                self._cfg.dsn, autocommit=True, application_name="pg-llm-describer"
            ) as conn:
                conn.execute(
                    sql.SQL("set lock_timeout = {}").format(
                        sql.Literal(self._cfg.lock_timeout)
                    )
                )
                conn.execute(
                    sql.SQL("set statement_timeout = {}").format(
                        sql.Literal(self._cfg.statement_timeout)
                    )
                )
                try:
                    rounds, written = await self._rounds(conn)
                finally:
                    self._unlock(conn)
                pruned = self._prune(conn)
                return CycleReport(rounds=rounds, written=written, pruned=pruned)
        except psycopg.Error as exc:
            raise DescriberWorkerError(f"ix database {self._cfg.dsn}: {exc}") from exc

    async def _rounds(self, conn: psycopg.Connection) -> tuple[int, int]:
        rounds = 0
        written = 0
        while True:
            rows = self._queue(conn)
            if not rows:
                break
            for row in rows:
                description = await self._describe(row)
                self._write(conn, row, description)
                written += 1
            self._unlock(conn)
            rounds += 1
            logger.info("round %d: %d objects described", rounds, len(rows))
        return rounds, written

    def _queue(self, conn: psycopg.Connection) -> list[QueueRow]:
        cur = conn.execute(
            self._sql.load(SqlFile.QUEUE),
            {"batch": self._cfg.batch, "indexer_hash": self._indexer_hash},
        )
        rows: list[QueueRow] = []
        for node_id, surface, text, input_hash in cur.fetchall():
            rows.append(
                QueueRow(
                    node_id=node_id, surface=surface, text=text, input_hash=input_hash
                )
            )
        return rows

    async def _describe(self, row: QueueRow) -> str:
        try:
            raw = await self._generator.generate(
                self._prompts.user(row.text), self._prompts.schema
            )
        except GenerationError as exc:
            raise DescriberWorkerError(f"describe node {row.node_id}: {exc}") from exc
        try:
            reply = Reply.model_validate_json(raw)
        except ValidationError as exc:
            raise DescriberWorkerError(
                f"describe node {row.node_id}: reply is not by schema: {raw[:200]!r}: "
                "{exc}"
            ) from exc
        return reply.description.strip()

    def _write(self, conn: psycopg.Connection, row: QueueRow, description: str) -> None:
        params = {
            "node_id": row.node_id,
            "surface": row.surface,
            "content": description,
            "input_hash": row.input_hash,
            "indexer_hash": self._indexer_hash,
        }
        conn.execute(self._sql.load(SqlFile.WRITE), params)

    def _unlock(self, conn: psycopg.Connection) -> None:
        conn.execute(self._sql.load(SqlFile.UNLOCK))

    def _prune(self, conn: psycopg.Connection) -> int:
        record = conn.execute(self._sql.load(SqlFile.PRUNE)).fetchone()
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
            prog="boba-pg-llm-describer",
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
                "должно быть уже накачено пакетом pg-ix-core); run — рабочий цикл."
            ),
        )
        parser.add_argument(
            "--config",
            required=True,
            type=Path,
            help=(
                "Путь к файлу конфига приложения (toml). Все настройки, включая "
                "строку подключения к базе ix, берутся из секции [ix.llm_describer]."
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
            upgrade = bind_section(config_path, Cli.SECTION, UpgradeConfig)
            report = SchemaUpgrade(package_dir / "schema").run(upgrade)
            logger.info("schema applied: %s", ", ".join(report.files))
            return

        cfg = bind_section(config_path, Cli.SECTION, WorkerConfig)
        here = Path(__file__).resolve().parent
        prompts = Prompts(here / "prompt")
        worker = DescriberWorker(
            cfg,
            PackageSql(here / "run", cfg.db_schema),
            prompts,
            Generators.build(cfg, prompts),
        )
        report = asyncio.run(worker.run())
        logger.info(
            "done: rounds=%d written=%d pruned=%d",
            report.rounds,
            report.written,
            report.pruned,
        )
    except (ConfigError, SchemaUpgradeError, DescriberWorkerError) as exc:
        raise SystemExit(str(exc)) from exc


if __name__ == "__main__":
    main()
