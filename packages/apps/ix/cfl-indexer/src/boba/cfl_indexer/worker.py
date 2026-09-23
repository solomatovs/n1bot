"""Индексатор Confluence в граф ix: процесс на спейс, внутри всё по порядку.

Родитель читает конфиг, собирает пары «источник, ключ спейса» и раздаёт их процессам:
по одному спейсу на процесс, процесс умирает вместе со спейсом и память возвращается
системе. Внутри процесса одно соединение к ix и один http-клиент: спейс, страницы и
блог-записи, у каждой тело, вложения и комментарии, потом рёбра и чистка — строго
друг за другом, без потоков и очередей. В ix_fts кладутся только body и ocr,
остальные аспекты выводят общие индексаторы.

Ошибки:
IndexerWorkerError — база ix, полнотекст, список спейсов, конфиг, модели OCR
    или процесс спейса, упавший не своей ошибкой. Ошибка Confluence или базы
    внутри спейса даёт отчёт с error, остальные спейсы идут дальше; файл
    вложения, который не скачался или не разобрался, считается в failed.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import multiprocessing
from collections.abc import Sequence
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import ClassVar

import httpx
import psycopg
from pydantic import BaseModel, ConfigDict, Field

from boba.cfl_indexer.confluence import (
    Attachment,
    AttachmentGoneError,
    Comment,
    ConfluenceReader,
    ConfluenceReadError,
    Content,
    Space,
    SpaceSelector,
)
from boba.cfl_indexer.documents import AttachmentReader, DocumentTextError
from boba.cfl_indexer.store import (
    Aspect,
    IxStore,
    RunFile,
    State,
    Surface,
)
from boba.config import bind_section
from boba.confluence.models import AttachmentVerdict
from boba.confluence.rest import ConfluenceConnection, ContentType
from boba.db.postgres import AsyncPostgresPool, PostgresError
from boba.db.postgres.names import PostgresSchema
from boba.doc.config import DocSection
from boba.doc.document import DocumentError
from boba.doc.ocr import OcrEngines
from boba.ix_core.database import IxDatabase, enter_kerberos
from boba.ix_core.upgrade import SchemaUpgrade
from boba.krb import KerberosWorkspaceConfig

__all__ = [
    "CflAddress",
    "ConfluenceSource",
    "Indexer",
    "IndexerCli",
    "IndexerConfig",
    "IndexerWorkerError",
    "Report",
    "Sources",
    "SpaceJob",
    "SpaceRun",
    "SpaceSelection",
    "SpaceWalker",
    "cli",
    "index_space",
    "main",
]

logger = logging.getLogger("cfl-indexer")

"""Поднимается при смене преобразования в коде: всё идёт на переиндексацию."""
LOG_FORMAT = "%(asctime)s %(name)s %(message)s"


class IndexerWorkerError(Exception):
    """Ошибка прогона: база ix, список спейсов, конфиг или процесс спейса."""


class ConfluenceSource(BaseModel):
    """Один сервер Confluence и его спейсы."""

    model_config = ConfigDict(frozen=True)

    name: str = Field(min_length=1)
    confluence: ConfluenceConnection
    spaces: SpaceSelector


class IndexerConfig(IxDatabase):
    """Секция [ix.cfl_indexer]."""

    sources: Sequence[ConfluenceSource] = Field(min_length=1)
    parallel_spaces: int = Field(ge=1, default=1)
    progress_every: int = Field(ge=1, default=100)
    list_limit: int = Field(ge=1)
    """Окно списков Confluence: страниц, вложений, комментариев за один запрос."""
    doc: DocSection
    """Таблица [ix.cfl_indexer.doc]: чтение вложений роутером boba-doc и OCR."""
    attachments: Sequence[str] = ()
    indexer_number: int = Field(ge=1, default=1)
    """Имя файла или media-type со слэшем; пусто — все."""


@dataclass(kw_only=True)
class Report:
    """Итог обхода спейса; error непустой — обход прерван."""

    space_key: str
    seen: int = 0
    indexed: int = 0
    unchanged: int = 0
    swept: int = 0
    failed: int = 0
    linked: int = 0
    """Пик RSS процесса спейса: по нему видно, копит ли обход память."""
    error: str = ""

    @property
    def ok(self) -> bool:
        if self.error:
            return False

        return self.failed == 0

    def line(self) -> str:
        text = (
            f"{self.space_key}: seen={self.seen} indexed={self.indexed} "
            f"unchanged={self.unchanged} linked={self.linked} swept={self.swept} "
            f"failed={self.failed}"
        )
        if self.error:
            text = f"{text} error={self.error}"

        return text


class Sources:
    """Источники конфига: выбор по маскам командной строки, поиск по имени и
    отпечаток параметров индексатора, от которых зависит текст индекса."""

    def __init__(self, cfg: IndexerConfig) -> None:
        self._cfg = cfg

    def select(self, source_name: str, space_key: str) -> list[ConfluenceSource]:
        """Источники прогона; один спейс задаётся только одному источнику."""
        sources = list(self._cfg.sources)
        if source_name:
            sources = [self.find(source_name)]

        if space_key and len(sources) != 1:
            raise IndexerWorkerError(
                f"--space {space_key} needs --source when the config lists "
                f"{len(sources)} sources"
            )

        return sources

    def find(self, name: str) -> ConfluenceSource:
        for item in self._cfg.sources:
            if item.name == name:
                return item

        known = ", ".join(item.name for item in self._cfg.sources)
        raise IndexerWorkerError(
            f"source {name!r} is not listed in the config, known sources: {known}"
        )

    def indexer_hash(self, source: ConfluenceSource) -> str:
        """md5 параметров, от которых зависит текст индекса; смена —
        переиндексация. indexer_number конфига сбрасывает хэш вручную."""
        material = {
            "layout": self._cfg.indexer_number,
            "body_format": source.confluence.body_format,
            "attachments": list(self._cfg.attachments),
            "text_encodings": list(self._cfg.doc.text_encodings),
            "ocr": dict(self._cfg.doc.ocr.fingerprint()),
        }
        encoded = json.dumps(material, sort_keys=True, ensure_ascii=False)

        return hashlib.md5(encoded.encode("utf-8"), usedforsecurity=False).hexdigest()


class CflAddress:
    """Адреса node частями: сервер, дальше ключ спейса или id объекта."""

    HTTPS_PORT: ClassVar[int] = 443
    HTTP_PORT: ClassVar[int] = 80

    def __init__(self, conn: ConfluenceConnection) -> None:
        root = httpx.URL(str(conn.profile.root_url()))
        port = root.port
        if port is None:
            port = self._default_port(root.scheme)

        self._base: dict[str, object] = {
            "scheme": root.scheme,
            "host": root.host,
            "port": port,
        }
        path = root.path.rstrip("/")
        if path:
            self._base["path"] = path

    def base(self) -> dict[str, object]:
        return dict(self._base)

    def of_space(self, key: str) -> dict[str, object]:
        return {**self._base, "space": key}

    def of_content(self, content_id: str) -> dict[str, object]:
        return {**self._base, "content": content_id}

    def of_attachment(self, content_id: str, attachment_id: str) -> dict[str, object]:
        return {**self._base, "content": content_id, "attachment": attachment_id}

    def of_comment(self, content_id: str, comment_id: str) -> dict[str, object]:
        return {**self._base, "content": content_id, "comment": comment_id}

    def _default_port(self, scheme: str) -> int:
        if scheme == "https":
            return self.HTTPS_PORT

        return self.HTTP_PORT


@dataclass(frozen=True, kw_only=True)
class SpaceJob:
    """Один спейс для процесса: источник, ключ и нужен ли полный переобход."""

    source_name: str
    space_key: str
    reindex: bool


class SpaceWalker:
    """Обход одного спейса: чтение, запись, счётчики."""

    def __init__(
        self,
        cfg: IndexerConfig,
        source: ConfluenceSource,
        job: SpaceJob,
        reader: ConfluenceReader,
        store: IxStore,
    ) -> None:
        self._space_key = job.space_key
        self._cfg = cfg
        self._reader = reader
        self._store = store
        self._reindex = job.reindex
        self._report = Report(space_key=self._space_key)
        self._address = CflAddress(source.confluence)
        self._indexer_hash = Sources(cfg).indexer_hash(source)
        self._attachments = AttachmentReader(cfg.doc, cfg.attachments)

    def get_report(self) -> Report:
        return self._report

    async def walk(self) -> None:
        await self._store.create_tables()

        space = await self._reader.read_space(self._space_key)
        space_node = await self.index_space_row(space)

        for kind in (ContentType.PAGE, ContentType.BLOGPOST):
            async for content in self._reader.iter_contents(self._space_key, kind):
                await self.index_content(content, space_node)

        base = self._address.base()
        self._report.linked = await self._store.apply_links(self._space_key, base)
        self._report.swept = await self._store.sweep_space(self._space_key, base)

    def has_changed(self, state: State | None, version: int) -> bool:
        """Сущность перечитывается, если её нет в базе, сменились параметры
        индексатора или версия в списке Confluence не та, что в строке;
        --reindex отключает отсечение."""
        if self._reindex:
            return True

        if state is None:
            return True

        if state.indexer_hash != self._indexer_hash:
            return True

        return state.version != version

    def same_bytes(self, state: State | None, content_hash: str) -> bool:
        """Версия сменилась, а сырой контент тот же: перезаписывать текст незачем."""
        if state is None:
            return False

        if state.indexer_hash != self._indexer_hash:
            return False

        return state.content_hash == content_hash

    async def register_node(
        self, surface: Surface, address: dict[str, object], parent: int | None
    ) -> int:
        """Node, tree и seen у любого объекта, иначе чистка снесёт его."""
        node_id = await self._store.upsert_node(surface, address)
        await self._store.attach_to_parent(node_id, parent)
        await self._store.mark_seen(node_id)
        self._report.seen += 1
        if self._report.seen % self._cfg.progress_every == 0:
            logger.info("progress: %s", self._report.line())

        return node_id

    async def index_space_row(self, space: Space) -> int:
        node_id = await self.register_node(
            Surface.SPACE, self._address.of_space(space.key), None
        )
        state = await self._store.read_state(RunFile.SPACE_STATE, node_id)
        unchanged = False
        if not self._reindex:
            unchanged = self.same_bytes(state, space.content_hash)

        if unchanged:
            self._report.unchanged += 1
            return node_id

        async with self._store.transaction():
            await self._store.write_space(node_id, space, self._indexer_hash)
            await self._store.clear_texts(node_id)

        self._report.indexed += 1

        return node_id

    async def index_content(self, content: Content, space_node: int) -> None:
        parent = space_node
        if content.parent_id:
            parent = await self._store.upsert_node(
                Surface.PAGE, self._address.of_content(content.parent_id)
            )

        node_id = await self.register_node(
            self._store.surface_of(content.kind),
            self._address.of_content(content.id),
            parent,
        )
        state = await self._store.read_state(
            self._store.state_file_of(content.kind), node_id
        )
        if self.has_changed(state, content.version):
            await self.write_body(node_id, await self._reader.read_body(content))
        else:
            self._report.unchanged += 1

        async for attachment in self._reader.iter_attachments(content):
            await self.index_attachment(attachment, node_id)

        async for comment in self._reader.iter_comments(content):
            await self.index_comment(comment, node_id)

    async def write_body(self, node_id: int, content: Content) -> None:
        surface = self._store.surface_of(content.kind)
        await self._store.queue_links(node_id, content.links)
        async with self._store.transaction():
            await self._store.write_content(node_id, content, self._indexer_hash)
            await self._store.clear_texts(node_id)
            await self._store.push_text(node_id, surface, Aspect.BODY, content.markdown)

        self._report.indexed += 1
        logger.info(
            "%s %s %r: version %d indexed",
            content.kind,
            content.id,
            content.title,
            content.version,
        )

    async def index_comment(self, comment: Comment, page_node: int) -> None:
        node_id = await self.register_node(
            Surface.COMMENT,
            self._address.of_comment(comment.page_id, comment.id),
            page_node,
        )
        state = await self._store.read_state(RunFile.COMMENT_STATE, node_id)
        if not self.has_changed(state, comment.version):
            self._report.unchanged += 1
            return

        async with self._store.transaction():
            await self._store.write_comment(node_id, comment, self._indexer_hash)
            await self._store.clear_texts(node_id)
            await self._store.push_text(
                node_id, Surface.COMMENT, Aspect.BODY, comment.markdown
            )

        self._report.indexed += 1
        logger.info(
            "comment %s on %s: version %d indexed",
            comment.id,
            comment.page_id,
            comment.version,
        )

    async def index_attachment(self, attachment: Attachment, page_node: int) -> None:
        node_id = await self.register_node(
            Surface.ATTACHMENT,
            self._address.of_attachment(attachment.page_id, attachment.id),
            page_node,
        )
        state = await self._store.read_state(RunFile.ATTACHMENT_STATE, node_id)
        if not self.has_changed(state, attachment.version):
            self._report.unchanged += 1
            return

        verdict = self._attachments.decide(attachment)
        if verdict is not AttachmentVerdict.TAKE:
            logger.info(
                "attachment %s %r (%s): %s, metadata only",
                attachment.id,
                attachment.title,
                attachment.media_type,
                verdict.value,
            )
            await self.write_attachment_text(node_id, attachment, "", Aspect.BODY, "")
            return

        await self.download_and_extract(node_id, attachment, state)

    async def download_and_extract(
        self, node_id: int, attachment: Attachment, state: State | None
    ) -> None:
        """Одним проходом: байты из http идут в ридер через пипу, sha256
        считается по дороге, на диск ничего не ложится. Перезалитый без
        изменений файл обновляет строку, но текст не трогает."""
        consume = self._attachments.consumer(attachment)
        try:
            content_hash, text = await self._reader.read_attachment(attachment, consume)
        except AttachmentGoneError:
            logger.info("attachment %s skipped: gone before download", attachment.id)
            return
        except ConfluenceReadError as exc:
            logger.error("attachment %s not downloaded: %s", attachment.id, exc)
            self._report.failed += 1
            return
        except DocumentTextError as exc:
            logger.error("attachment %s not indexed: %s", attachment.id, exc)
            self._report.failed += 1
            return

        if self.same_bytes(state, content_hash):
            await self._store.write_attachment(
                node_id, attachment, content_hash, self._indexer_hash
            )
            self._report.unchanged += 1
            return

        aspect = self._attachments.aspect(attachment)
        await self.write_attachment_text(
            node_id, attachment, content_hash, aspect, text
        )

    async def write_attachment_text(
        self,
        node_id: int,
        attachment: Attachment,
        content_hash: str,
        aspect: Aspect,
        text: str,
    ) -> None:
        async with self._store.transaction():
            await self._store.write_attachment(
                node_id, attachment, content_hash, self._indexer_hash
            )
            await self._store.clear_texts(node_id)
            await self._store.push_text(node_id, Surface.ATTACHMENT, aspect, text)

        self._report.indexed += 1
        logger.info(
            "attachment %s %r (%s): version %d indexed, %d chars",
            attachment.id,
            attachment.title,
            attachment.media_type,
            attachment.version,
            len(text),
        )


class SpaceRun:
    """Обход одного спейса в его процессе: соединение к ix, ридер Confluence и
    обходчик живут ровно столько, сколько идёт спейс. Ошибка Confluence или
    базы прерывает спейс и уходит в отчёт полем error."""

    def __init__(self, cfg: IndexerConfig, job: SpaceJob, package_dir: Path) -> None:
        self._cfg = cfg
        self._job = job
        self._package_dir = package_dir
        self._sources = Sources(cfg)
        self._source = self._sources.find(job.source_name)

    async def run(self) -> Report:
        key = self._job.space_key
        report = Report(space_key=key)
        try:
            async with (
                await AsyncPostgresPool.dedicated(self._cfg.postgres) as conn,
                ConfluenceReader(
                    self._source.confluence, self._source.spaces, self._cfg.list_limit
                ) as reader,
            ):
                store = IxStore(self._package_dir, self._cfg.db_schema, conn)
                walker = SpaceWalker(self._cfg, self._source, self._job, reader, store)
                try:
                    await walker.walk()
                finally:
                    report = walker.get_report()
        except Exception as exc:
            logger.error("space %s aborted: %s", key, exc)
            report.error = str(exc)

        logger.info("space %s", report.line())

        return report


def index_space(
    cfg: IndexerConfig,
    job: SpaceJob,
    package_dir: Path,
    krb: KerberosWorkspaceConfig | None,
) -> Report:
    """Вход процесса спейса: свой лог, свой каталог kerberos, свой event loop,
    один спейс. Функция уровня модуля, потому что её сериализует пул процессов."""
    logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
    if krb is not None:
        krb.apply()

    return asyncio.run(SpaceRun(cfg, job, package_dir).run())


@dataclass(frozen=True, kw_only=True)
class SpaceSelection:
    """Что обходить: маска источника и спейса из командной строки, полный
    переобход; пустая маска значит все."""

    source: str = ""
    space: str = ""
    reindex: bool = False


class Indexer:
    """Прогон индексатора: проверка полнотекста и моделей OCR, цели по
    источникам и раздача спейсов процессам, по одному спейсу на процесс.

    Создаётся точкой входа из конфига; отчёты идут в порядке спейсов, спейс с
    ошибкой даёт отчёт с error, а процесс, упавший не своей ошибкой, роняет
    прогон IndexerWorkerError.
    """

    FTS_TABLE: ClassVar[str] = "ix_fts"

    def __init__(
        self,
        cfg: IndexerConfig,
        package_dir: Path,
        krb: KerberosWorkspaceConfig | None,
    ) -> None:
        self._cfg = cfg
        self._package_dir = package_dir
        self._krb = krb
        self._sources = Sources(cfg)

    async def check_fts(self) -> None:
        """ix_fts накатывает пакет ix-fts; без него текст класть некуда."""
        async with await AsyncPostgresPool.dedicated(self._cfg.postgres) as conn:
            if await PostgresSchema.exists(conn, self._cfg.db_schema, self.FTS_TABLE):
                return

        raise IndexerWorkerError(
            f"table {self._cfg.db_schema}.{self.FTS_TABLE} is missing: apply the "
            "full-text index first: boba-ix-fts upgrade --config <config>"
        )

    async def targets(
        self, selection: SpaceSelection
    ) -> list[tuple[ConfluenceSource, str]]:
        """Пары источник и ключ спейса, по которым пойдёт прогон."""
        targets: list[tuple[ConfluenceSource, str]] = []
        for source in self._sources.select(selection.source, selection.space):
            if selection.space:
                targets.append((source, selection.space))
                continue

            async with ConfluenceReader(
                source.confluence, source.spaces, self._cfg.list_limit
            ) as reader:
                async for key in reader.list_space_keys():
                    logger.info("source: %s, space: %s", source.name, key)
                    targets.append((source, key))

        logger.info("total spaces: %d", len(targets))

        return targets

    async def run(self, selection: SpaceSelection) -> list[Report]:
        try:
            OcrEngines().check(self._cfg.doc.ocr)
            await self.check_fts()
            targets = await self.targets(selection)
        except DocumentError as exc:
            raise IndexerWorkerError(str(exc)) from exc
        except PostgresError as exc:
            raise IndexerWorkerError(
                f"ix database {self._cfg.postgres.where()}: {exc}"
            ) from exc
        except ConfluenceReadError as exc:
            raise IndexerWorkerError(str(exc)) from exc
        except psycopg.Error as exc:
            raise IndexerWorkerError(
                f"ix database {self._cfg.postgres.where()}: {exc}"
            ) from exc

        return await asyncio.to_thread(self._run_processes, targets, selection)

    def _run_processes(
        self,
        targets: Sequence[tuple[ConfluenceSource, str]],
        selection: SpaceSelection,
    ) -> list[Report]:
        """Процесс на спейс, parallel_spaces процессов разом; ожидание итогов
        блокирует, поэтому идёт в потоке рядом с циклом событий."""
        reports: list[Report] = []
        with ProcessPoolExecutor(
            max_workers=self._cfg.parallel_spaces,
            mp_context=multiprocessing.get_context("spawn"),
            max_tasks_per_child=1,
        ) as pool:
            futures = []
            for found, key in targets:
                job = SpaceJob(
                    source_name=found.name, space_key=key, reindex=selection.reindex
                )
                futures.append(
                    pool.submit(
                        index_space, self._cfg, job, self._package_dir, self._krb
                    )
                )

            for (found, key), future in zip(targets, futures, strict=True):
                try:
                    reports.append(future.result())
                except Exception as exc:
                    raise IndexerWorkerError(
                        f"space {key} of {found.name}: the space process failed: {exc}"
                    ) from exc

        return reports


class Command(StrEnum):
    UPGRADE = "upgrade"
    RUN = "run"


class IndexerCli:
    """Командная строка индексатора: разбор аргументов, накат схемы или прогон."""

    SECTION: ClassVar[str] = "ix.cfl_indexer"

    def __init__(self, argv: Sequence[str] | None = None) -> None:
        self._args = self._parse(argv)
        self._package_dir = Path(__file__).resolve().parent

    @staticmethod
    def _parse(argv: Sequence[str] | None) -> argparse.Namespace:
        parser = argparse.ArgumentParser(
            prog="boba-cfl-indexer",
            description=(
                "Индексатор Confluence в граф ix: схема пакета и обход спейсов."
            ),
        )
        parser.add_argument(
            "command",
            type=Command,
            choices=list(Command),
            help="upgrade — накатить схему пакета в базу ix; run — обход спейсов.",
        )
        parser.add_argument(
            "--config",
            required=True,
            type=Path,
            help="Файл конфига приложения (toml), секция [ix.cfl_indexer].",
        )
        parser.add_argument(
            "--source", default="", help="Только этот источник из sources конфига."
        )
        parser.add_argument(
            "--space", default="", help="Только этот спейс; вместе с --source."
        )
        parser.add_argument(
            "--reindex",
            action="store_true",
            help="Перечитать все тела, не отсекая по версии и хэшу.",
        )

        return parser.parse_args(argv)

    async def run(self) -> None:
        args = self._args
        krb = enter_kerberos(args.config)
        if args.command is Command.UPGRADE:
            database = bind_section(args.config, self.SECTION, IxDatabase)
            upgrade = SchemaUpgrade(self._package_dir / "schema")
            report = await upgrade.run(database)
            logger.info("schema applied: %s", ", ".join(report.files))
            return

        cfg = bind_section(args.config, self.SECTION, IndexerConfig)
        indexer = Indexer(cfg, self._package_dir / "run", krb)
        selection = SpaceSelection(
            source=args.source, space=args.space, reindex=args.reindex
        )
        for report in await indexer.run(selection):
            logger.info("done: %s", report.line())


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
    try:
        await IndexerCli().run()
    except Exception as exc:
        raise SystemExit(str(exc)) from exc


def cli() -> None:
    """Точка входа консольного скрипта: единственный asyncio.run на процесс."""
    asyncio.run(main())


if __name__ == "__main__":
    asyncio.run(main())
