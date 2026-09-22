"""Индексатор Confluence в граф ix: процесс на спейс, внутри всё по порядку.

Родитель читает конфиг, собирает пары «источник, ключ спейса» и раздаёт их процессам:
по одному спейсу на процесс, процесс умирает вместе со спейсом и память возвращается
системе. Внутри процесса одно соединение к ix и один http-клиент: спейс, страницы и
блог-записи, у каждой тело, вложения и комментарии, потом рёбра и чистка — строго
друг за другом, без потоков и очередей. В ix_fts кладутся только body и ocr,
остальные аспекты выводят общие индексаторы.

Ошибки:
IndexerWorkerError — база ix, полнотекст, список спейсов, конфиг или процесс спейса,
    упавший не своей ошибкой. Ошибка Confluence или базы внутри спейса даёт отчёт с
    error, остальные спейсы идут дальше; файл вложения, который не скачался или не
    разобрался, считается в failed.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import multiprocessing
import resource
import tempfile
from collections.abc import Sequence
from concurrent.futures import ProcessPoolExecutor
from enum import StrEnum
from pathlib import Path

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
from boba.cfl_indexer.documents import (
    DocumentTextError,
    aspect_of,
    build_gate,
    decide_attachment,
    extract_text,
)
from boba.cfl_indexer.store import (
    Aspect,
    IxStore,
    IxWriteError,
    RunFile,
    State,
    Surface,
    state_file_of,
    surface_of,
)
from boba.config import ConfigError, bind_section
from boba.confluence.models import AttachmentVerdict
from boba.confluence.rest import ConfluenceConnection, ContentType
from boba.ix_core.database import IxDatabase, IxDatabaseError, IxPool
from boba.ix_core.schema_name import SchemaName
from boba.ix_core.upgrade import SchemaUpgrade, SchemaUpgradeError
from boba.text.document import LiteParseParams

__all__ = [
    "CflAddress",
    "ConfluenceSource",
    "IndexerConfig",
    "IndexerWorkerError",
    "Report",
    "SpaceWalk",
    "compute_indexer_hash",
    "index_space",
    "list_targets",
    "run_spaces",
]

logger = logging.getLogger("cfl-indexer")

INDEXER_LAYOUT = 2
"""Поднимается при смене преобразования в коде: всё идёт на переиндексацию."""
FTS_TABLE = "ix_fts"
CONTENT_KINDS = (ContentType.PAGE, ContentType.BLOGPOST)
SPACE_ERRORS = (ConfluenceReadError, IxWriteError, psycopg.Error)
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
    parser: LiteParseParams
    attachments: Sequence[str] = ()
    """Имя файла или media-type со слэшем; пусто — все."""
    text_encodings: Sequence[str] = Field(min_length=1, default=("utf-8",))


class Report(BaseModel):
    """Итог обхода спейса; error непустой — обход прерван."""

    space_key: str
    seen: int = 0
    indexed: int = 0
    unchanged: int = 0
    swept: int = 0
    failed: int = 0
    linked: int = 0
    peak_rss_mib: int = 0
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
            f"failed={self.failed} rss={self.peak_rss_mib}MiB"
        )
        if self.error:
            text = f"{text} error={self.error}"

        return text


def select_sources(
    cfg: IndexerConfig, source_name: str, space_key: str
) -> list[ConfluenceSource]:
    """Источники прогона; один спейс задаётся только одному источнику."""
    sources = list(cfg.sources)
    if source_name:
        sources = [find_source(cfg, source_name)]

    if space_key and len(sources) != 1:
        raise IndexerWorkerError(
            f"--space {space_key} needs --source when the config lists "
            f"{len(sources)} sources"
        )

    return sources


def find_source(cfg: IndexerConfig, name: str) -> ConfluenceSource:
    for item in cfg.sources:
        if item.name == name:
            return item

    known = ", ".join(item.name for item in cfg.sources)
    raise IndexerWorkerError(
        f"source {name!r} is not listed in the config, known sources: {known}"
    )


def compute_indexer_hash(cfg: IndexerConfig, source: ConfluenceSource) -> str:
    """md5 параметров, от которых зависит текст индекса; смена — переиндексация."""
    material = {
        "layout": INDEXER_LAYOUT,
        "body_format": source.confluence.body_format,
        "attachments": list(cfg.attachments),
        "text_encodings": list(cfg.text_encodings),
        "ocr_enabled": cfg.parser.ocr_enabled,
        "ocr_language": cfg.parser.ocr_language,
        "max_pages": cfg.parser.max_pages,
    }
    encoded = json.dumps(material, sort_keys=True, ensure_ascii=False)

    return hashlib.md5(encoded.encode("utf-8"), usedforsecurity=False).hexdigest()


class CflAddress:
    """Адреса node частями: сервер, дальше ключ спейса или id объекта."""

    def __init__(self, conn: ConfluenceConnection) -> None:
        root = httpx.URL(str(conn.profile.root_url()))
        port = root.port
        if port is None:
            port = default_port(root.scheme)

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


def default_port(scheme: str) -> int:
    if scheme == "https":
        return 443

    return 80


def peak_rss_mib() -> int:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss >> 10


class SpaceWalk:
    """Обход одного спейса: чтение, запись, счётчики."""

    def __init__(
        self,
        cfg: IndexerConfig,
        source: ConfluenceSource,
        reader: ConfluenceReader,
        store: IxStore,
        *,
        reindex: bool,
    ) -> None:
        self._cfg = cfg
        self._reader = reader
        self._store = store
        self._reindex = reindex
        self._report = Report(space_key="")
        self._address = CflAddress(source.confluence)
        self._indexer_hash = compute_indexer_hash(cfg, source)
        self._gate = build_gate(cfg.attachments, ocr=cfg.parser.ocr_enabled)

    @property
    def report(self) -> Report:
        return self._report

    async def walk(self, space_key: str) -> None:
        self._report.space_key = space_key
        await self._store.create_run_tables()
        space_node = await self.index_space_row(
            await self._reader.read_space(space_key)
        )

        for kind in CONTENT_KINDS:
            async for content in self._reader.iter_contents(space_key, kind):
                await self.index_content(content, space_node)

        base = self._address.base()
        self._report.linked = await self._store.apply_links(space_key, base)
        self._report.swept = await self._store.sweep_space(space_key, base)

    def has_changed(self, state: State | None, version: int) -> bool:
        """--reindex отключает отсечение на прогон."""
        if self._reindex:
            return True

        if state is None:
            return True

        if state.version != version:
            return True

        return state.indexer_hash != self._indexer_hash

    async def register_node(
        self, surface: Surface, address: dict[str, object], parent: int | None
    ) -> int:
        """Node, tree и seen у любого объекта, иначе чистка снесёт его."""
        node_id = await self._store.upsert_node(surface, address)
        await self._store.attach_to_parent(node_id, parent)
        await self._store.mark_seen(node_id)
        self._report.seen += 1
        if self._report.seen % self._cfg.progress_every == 0:
            self._report.peak_rss_mib = peak_rss_mib()
            logger.info("progress: %s", self._report.line())

        return node_id

    async def index_space_row(self, space: Space) -> int:
        node_id = await self.register_node(
            Surface.SPACE, self._address.of_space(space.key), None
        )
        state = await self._store.read_state(RunFile.SPACE_STATE, node_id)
        if self.same_bytes(state, space.content_hash):
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
            surface_of(content.kind), self._address.of_content(content.id), parent
        )
        state = await self._store.read_state(state_file_of(content.kind), node_id)
        if self.has_changed(state, content.version):
            await self.write_body(node_id, await self._reader.read_body(content))
        else:
            self._report.unchanged += 1

        async for attachment in self._reader.iter_attachments(content):
            await self.index_attachment(attachment, node_id)

        async for comment in self._reader.iter_comments(content):
            await self.index_comment(comment, node_id)

    async def write_body(self, node_id: int, content: Content) -> None:
        surface = surface_of(content.kind)
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

        verdict = decide_attachment(attachment, self._gate)
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
        """Скачать; при тех же байтах и параметрах текст остаётся, иначе извлечь."""
        with tempfile.TemporaryDirectory(prefix="cfl-indexer-") as spool:
            path = Path(spool) / "attachment"
            try:
                content_hash = await self._reader.download_attachment(attachment, path)
            except AttachmentGoneError:
                logger.info(
                    "attachment %s skipped: gone before download", attachment.id
                )
                return
            except ConfluenceReadError as exc:
                logger.error("attachment %s not downloaded: %s", attachment.id, exc)
                self._report.failed += 1
                return

            if self.same_bytes(state, content_hash):
                await self._store.write_attachment(
                    node_id, attachment, content_hash, self._indexer_hash
                )
                self._report.indexed += 1
                logger.info(
                    "attachment %s %r: version %d, same bytes, text kept",
                    attachment.id,
                    attachment.title,
                    attachment.version,
                )
                return

            try:
                text = extract_text(
                    path, attachment, self._cfg.text_encodings, self._cfg.parser
                )
            except DocumentTextError as exc:
                logger.error("attachment %s not indexed: %s", attachment.id, exc)
                self._report.failed += 1
                return

        aspect = aspect_of(attachment.media_type)
        await self.write_attachment_text(
            node_id, attachment, content_hash, aspect, text
        )

    def same_bytes(self, state: State | None, content_hash: str) -> bool:
        """Оригинал и параметры те же, что в прошлый прогон; --reindex это отключает."""
        if self._reindex:
            return False

        if state is None:
            return False

        if state.content_hash != content_hash:
            return False

        return state.indexer_hash == self._indexer_hash

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


async def walk_space(
    cfg: IndexerConfig,
    source: ConfluenceSource,
    space_key: str,
    package_dir: Path,
    *,
    reindex: bool,
) -> Report:
    report = Report(space_key=space_key)
    try:
        async with (
            IxPool.session(cfg) as conn,
            ConfluenceReader(source.confluence) as reader,
        ):
            store = IxStore(package_dir, cfg.db_schema, conn)
            walk = SpaceWalk(cfg, source, reader, store, reindex=reindex)
            try:
                await walk.walk(space_key)
            finally:
                report = walk.report
    except SPACE_ERRORS as exc:
        logger.error("space %s aborted: %s", space_key, exc)
        report.error = str(exc)
    except IxDatabaseError as exc:
        logger.error("space %s aborted: %s", space_key, exc)
        report.error = str(exc)

    report.peak_rss_mib = peak_rss_mib()
    logger.info("space %s", report.line())

    return report


def index_space(
    cfg: IndexerConfig,
    source_name: str,
    space_key: str,
    package_dir: Path,
    reindex: bool,
) -> Report:
    """Вход процесса спейса: свой лог, свой event loop, один спейс."""
    logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
    source = find_source(cfg, source_name)

    return asyncio.run(walk_space(cfg, source, space_key, package_dir, reindex=reindex))


async def list_targets(
    cfg: IndexerConfig, source_name: str, space_key: str
) -> list[tuple[ConfluenceSource, str]]:
    """Маска без glob — ключ как есть, со звёздочкой — список сервера."""
    targets: list[tuple[ConfluenceSource, str]] = []
    for source in select_sources(cfg, source_name, space_key):
        if space_key:
            targets.append((source, space_key))
            continue

        async with ConfluenceReader(source.confluence) as reader:
            keys = await reader.list_space_keys(source.spaces)

        logger.info("source %s: %d space(s) to walk", source.name, len(keys))
        for key in keys:
            targets.append((source, key))

    return targets


async def check_fts(cfg: IndexerConfig) -> None:
    """ix_fts накатывает пакет ix-fts; без него текст класть некуда."""
    async with IxPool.session(cfg) as conn:
        if await SchemaName.exists(conn, cfg.db_schema, FTS_TABLE):
            return

    raise IndexerWorkerError(
        f"table {cfg.db_schema}.{FTS_TABLE} is missing: apply the full-text index "
        "first: boba-ix-fts upgrade --config <config>"
    )


def run_spaces(
    cfg: IndexerConfig,
    package_dir: Path,
    *,
    source: str = "",
    space: str = "",
    reindex: bool = False,
) -> list[Report]:
    """Прогон по спейсам: процесс на спейс, parallel_spaces процессов разом.
    Отчёты в порядке спейсов; спейс с ошибкой — отчёт с error."""
    try:
        asyncio.run(check_fts(cfg))
        targets = asyncio.run(list_targets(cfg, source, space))
    except IxDatabaseError as exc:
        raise IndexerWorkerError(str(exc)) from exc
    except ConfluenceReadError as exc:
        raise IndexerWorkerError(str(exc)) from exc
    except psycopg.Error as exc:
        raise IndexerWorkerError(f"ix database {cfg.postgres.where()}: {exc}") from exc

    reports: list[Report] = []
    with ProcessPoolExecutor(
        max_workers=cfg.parallel_spaces,
        mp_context=multiprocessing.get_context("spawn"),
        max_tasks_per_child=1,
    ) as pool:
        futures = []
        for found, key in targets:
            futures.append(
                pool.submit(index_space, cfg, found.name, key, package_dir, reindex)
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


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="boba-cfl-indexer",
        description="Индексатор Confluence в граф ix: схема пакета и обход спейсов.",
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


def main() -> None:
    logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
    section = "ix.cfl_indexer"
    package_dir = Path(__file__).resolve().parent

    try:
        args = parse_args()
        if args.command is Command.UPGRADE:
            database = bind_section(args.config, section, IxDatabase)
            upgrade = SchemaUpgrade(package_dir / "schema")
            report = asyncio.run(upgrade.run(database))
            logger.info("schema applied: %s", ", ".join(report.files))
            return

        cfg = bind_section(args.config, section, IndexerConfig)
        reports = run_spaces(
            cfg,
            package_dir / "run",
            source=args.source,
            space=args.space,
            reindex=args.reindex,
        )
        for report in reports:
            logger.info("done: %s", report.line())
    except (ConfigError, SchemaUpgradeError, IndexerWorkerError) as exc:
        raise SystemExit(str(exc)) from exc


if __name__ == "__main__":
    main()
