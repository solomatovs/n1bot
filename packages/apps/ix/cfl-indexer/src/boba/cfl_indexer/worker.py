"""Индексатор Confluence в граф ix: скрапер поверхностей и текста.

Один спейс обрабатывает один воркер строго последовательно: спейс, затем страницы и
блог-записи по списку без тел, на каждом объекте — node, tree, surface-строка и текст,
который SQL добыть не может (markdown страницы, текст вложения, OCR), в общий
{schema}.ix_fts; после страницы идут её вложения и комментарии тем же порядком.
Оригиналы не хранятся, только их хэши. Несколько спейсов на входе — несколько воркеров
параллельно, у каждого своё соединение из пула. В конце обхода невиденные node
снимаются, а ссылки становятся рёбрами.

Остальное делают общие индексаторы по объявлениям аспектов: ix-fts выводит title, path
и card и выравнивает веса, ix-trgm и ix-vector берут свои классы. Поэтому аспект,
объявленный описателем или другим потребителем, попадает в индексы сам, без обхода
Confluence, а этот индексатор не знает ни весов, ни модели эмбеддинга.

Отсечение работы: version и indexer_hash совпали с surface-строкой — объект не
трогается; version сменился — тело скачивается, и content_hash оригинала решает, что
переписать.

Ошибки:
IndexerWorkerError — Confluence или база ix недоступны, ответ не того вида, что
    ожидался, или конфиг противоречив (спейсов в полёте больше, чем соединений в
    пуле). Файл вложения, который не разобран, в ошибку не превращается: он
    считается в failed отчёта спейса.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import tempfile
from collections.abc import Sequence
from enum import StrEnum
from pathlib import Path
from typing import Any, ClassVar, Self

import httpx
import psycopg
from pydantic import BaseModel, ConfigDict, Field, model_validator

from boba.cfl_indexer.confluence import (
    AttachmentGoneError,
    AttachmentSummary,
    CommentDocument,
    ConfluenceReadError,
    ContentBody,
    ContentSummary,
    SpaceDocument,
    SpaceReader,
    SpaceSelector,
    TextOf,
)
from boba.cfl_indexer.documents import AttachmentText, DocumentTextError, TextParams
from boba.cfl_indexer.store import (
    Aspect,
    IxWriteError,
    IxWriter,
    NodeState,
    PackageSql,
    PushedText,
    RowWrite,
    RunFile,
    Surface,
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
    "IndexerHash",
    "IndexerWorker",
    "IndexerWorkerError",
    "SpaceIndexer",
    "SpaceReport",
    "SpaceTarget",
]

logger = logging.getLogger("cfl-indexer")


class IndexerWorkerError(Exception):
    """Ошибка прогона: Confluence, база ix или конфиг."""


class ConfluenceSource(BaseModel):
    """Один сервер Confluence и его спейсы: endpoint (профиль с auth или без,
    формат тела, дамп) и выбор спейсов масками."""

    model_config = ConfigDict(frozen=True)

    name: str = Field(min_length=1)
    confluence: ConfluenceConnection
    spaces: SpaceSelector


class SpaceTarget(BaseModel):
    """Спейс одного источника — единица работы воркера."""

    model_config = ConfigDict(frozen=True)

    source: ConfluenceSource
    key: str


class IndexerConfig(IxDatabase):
    """Секция [ix.cfl_indexer]: база ix, источники Confluence со спейсами, сколько
    спейсов идёт параллельно, маски вложений, кодировки текстовых файлов и таблица
    parser с настройками liteparse.

    Ни модели, ни весов здесь нет: индексатор кладёт в общий полнотекст только текст,
    который добыл сам, а веса, триграммы и векторы делают общие индексаторы ix-fts,
    ix-trgm и ix-vector по объявлениям. Настройки парсера лежат своей таблицей: у
    liteparse своя пара параллелизма (parser.num_workers — потоки OCR), и в одном
    уровне она путалась бы с parallel_spaces.
    """

    sources: Sequence[ConfluenceSource] = Field(min_length=1)
    parallel_spaces: int = Field(ge=1, default=1)
    """Сколько спейсов обходится одновременно; каждому нужно своё соединение."""
    parser: LiteParseParams
    attachments: Sequence[str] = ()
    """Маски взятых вложений: имя файла или media-type со слэшем; пусто — все."""
    text_encodings: Sequence[str] = Field(min_length=1, default=("utf-8",))

    def text_params(self) -> TextParams:
        return TextParams(
            masks=self.attachments,
            encodings=self.text_encodings,
            liteparse=self.parser,
        )

    def source(self, name: str) -> ConfluenceSource:
        for item in self.sources:
            if item.name == name:
                return item

        known = ", ".join(item.name for item in self.sources)
        raise IndexerWorkerError(
            f"source {name!r} is not listed in the config, known sources: {known}"
        )

    def selected(self, source_name: str, space_key: str) -> list[ConfluenceSource]:
        """Источники прогона; один спейс задаётся только одному источнику."""
        sources = list(self.sources)
        if source_name:
            sources = [self.source(source_name)]

        if space_key and len(sources) != 1:
            raise IndexerWorkerError(
                f"--space {space_key} needs --source when the config lists "
                f"{len(sources)} sources"
            )

        return sources

    @model_validator(mode="after")
    def _spaces_fit_pool(self) -> Self:
        """Каждому спейсу в полёте нужно своё соединение; пул без потолка подходит
        любому числу."""
        ceiling = self.postgres.pool.max_size
        if ceiling is None:
            return self

        if self.parallel_spaces > ceiling:
            raise ValueError(
                f"parallel_spaces = {self.parallel_spaces} needs "
                f"postgres.pool.max_size >= {self.parallel_spaces}, got {ceiling}"
            )

        return self


class IndexerHash:
    """md5 параметров, от которых зависит содержимое индекса: смена любого переводит
    все объекты в переиндексацию, и так как оригиналы не хранятся, спейс качается
    заново."""

    LAYOUT: ClassVar[int] = 2
    """Версия раскладки текста; поднимается при смене преобразования в коде."""

    @classmethod
    def of(cls, cfg: IndexerConfig, source: ConfluenceSource) -> str:
        material = {
            "layout": cls.LAYOUT,
            "body_format": source.confluence.body_format,
            "heading_style": TextOf.HEADING_STYLE,
            "attachments": list(cfg.attachments),
            "text_encodings": list(cfg.text_encodings),
            "ocr_enabled": cfg.parser.ocr_enabled,
            "ocr_language": cfg.parser.ocr_language,
            "max_pages": cfg.parser.max_pages,
        }
        encoded = json.dumps(material, sort_keys=True, ensure_ascii=False)

        return hashlib.md5(encoded.encode("utf-8"), usedforsecurity=False).hexdigest()


class CflAddress:
    """Адреса node Confluence частями: схема, хост и порт из профиля, дальше ключ спейса
    или id контента. Страница адресуется id без ключа спейса: перенос между спейсами
    не рождает новый node."""

    SCHEME = "scheme"
    HOST = "host"
    PORT = "port"
    PATH = "path"
    SPACE = "space"
    CONTENT = "content"
    ATTACHMENT = "attachment"
    COMMENT = "comment"

    def __init__(self, conn: ConfluenceConnection) -> None:
        root = httpx.URL(str(conn.profile.root_url()))
        port = root.port
        if port is None:
            port = self._default_port(root.scheme)

        self._base: dict[str, object] = {
            self.SCHEME: root.scheme,
            self.HOST: root.host,
            self.PORT: port,
        }
        # Confluence под префиксом (/confluence у cwiki) — другой сервер на том же хосте
        path = root.path.rstrip("/")
        if path:
            self._base[self.PATH] = path

    @staticmethod
    def _default_port(scheme: str) -> int:
        if scheme == "https":
            return 443

        return 80

    @property
    def base(self) -> dict[str, object]:
        """Части адреса сервера: по ним чистка отличает одноимённые спейсы разных
        Confluence."""
        return dict(self._base)

    def space(self, key: str) -> dict[str, object]:
        return {**self._base, self.SPACE: key}

    def content(self, content_id: str) -> dict[str, object]:
        return {**self._base, self.CONTENT: content_id}

    def attachment(self, content_id: str, attachment_id: str) -> dict[str, object]:
        return {**self._base, self.CONTENT: content_id, self.ATTACHMENT: attachment_id}

    def comment(self, content_id: str, comment_id: str) -> dict[str, object]:
        return {**self._base, self.CONTENT: content_id, self.COMMENT: comment_id}


class SpaceReport(BaseModel):
    """Итог обхода одного спейса."""

    model_config = ConfigDict(frozen=True)

    space_key: str
    seen: int
    indexed: int
    unchanged: int
    swept: int
    failed: int
    linked: int


class DownloadedFile(BaseModel):
    """Файл вложения на диске и sha256 его байтов."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    attachment: AttachmentSummary
    path: Path
    content_hash: str


class AttachmentWrite(BaseModel):
    """Что ложится в surface-строку и полнотекст вложения."""

    model_config = ConfigDict(frozen=True)

    attachment: AttachmentSummary
    content_hash: str
    pushed: Sequence[PushedText]


class SpaceCounters(BaseModel):
    """Счётчики обхода; растут по ходу и складываются в SpaceReport."""

    seen: int = 0
    indexed: int = 0
    unchanged: int = 0
    failed: int = 0

    def report(self, space_key: str, swept: int, linked: int) -> SpaceReport:
        return SpaceReport(
            space_key=space_key,
            seen=self.seen,
            indexed=self.indexed,
            unchanged=self.unchanged,
            swept=swept,
            failed=self.failed,
            linked=linked,
        )


class ContentFiles:
    """Файлы состояния и записи surface-строки по виду контента."""

    @staticmethod
    def surface(kind: ContentType) -> Surface:
        if kind is ContentType.BLOGPOST:
            return Surface.BLOGPOST

        return Surface.PAGE

    @staticmethod
    def state(kind: ContentType) -> RunFile:
        if kind is ContentType.BLOGPOST:
            return RunFile.BLOGPOST_STATE

        return RunFile.PAGE_STATE

    @staticmethod
    def upsert(kind: ContentType) -> RunFile:
        if kind is ContentType.BLOGPOST:
            return RunFile.BLOGPOST

        return RunFile.PAGE


class SpaceScope(BaseModel):
    """Что у обхода спейса от его источника: адреса сервера, хэш параметров и
    режим полного перечита."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    address: CflAddress
    indexer_hash: str
    reindex: bool


class SpaceIndexer:
    """Обход одного спейса одним соединением: спейс, страницы, блог-записи, чистка."""

    KINDS: ClassVar[tuple[ContentType, ...]] = (ContentType.PAGE, ContentType.BLOGPOST)

    def __init__(
        self,
        reader: SpaceReader,
        writer: IxWriter,
        texts: AttachmentText,
        scope: SpaceScope,
    ) -> None:
        self._reader = reader
        self._writer = writer
        self._texts = texts
        self._address = scope.address
        self._indexer_hash = scope.indexer_hash
        self._reindex = scope.reindex

    def _unchanged(self, state: NodeState | None, version: int) -> bool:
        """Отсечение по версии и параметрам; --reindex отключает его на прогон."""
        if self._reindex:
            return False

        if state is None:
            return False

        return state.unchanged(version, self._indexer_hash)

    async def run(
        self, conn: psycopg.AsyncConnection[Any], space_key: str
    ) -> SpaceReport:
        counters = SpaceCounters()
        await self._writer.seen_table(conn)

        space = await self._reader.space(space_key)
        space_node = await self._space(conn, space, counters)

        for kind in self.KINDS:
            async for summary in self._reader.contents(space_key, kind):
                await self._content(conn, summary, space_node, counters)

        linked = await self._writer.apply_links(conn, space_key, self._address.base)
        swept = await self._writer.sweep(conn, space_key, self._address.base)
        report = counters.report(space_key, swept, linked)
        logger.info(
            "space %s: seen=%d indexed=%d unchanged=%d linked=%d swept=%d failed=%d",
            space_key,
            report.seen,
            report.indexed,
            report.unchanged,
            report.linked,
            report.swept,
            report.failed,
        )

        return report

    async def _space(
        self,
        conn: psycopg.AsyncConnection[Any],
        space: SpaceDocument,
        counters: SpaceCounters,
    ) -> int:
        node_id = await self._writer.node(
            conn, Surface.SPACE, self._address.space(space.key)
        )
        await self._writer.tree(conn, node_id, None)
        await self._writer.seen(conn, node_id)
        counters.seen += 1

        state = await self._writer.state(conn, RunFile.SPACE_STATE, node_id)
        if state is not None and state.content_hash == space.content_hash:
            if state.indexer_hash == self._indexer_hash and not self._reindex:
                counters.unchanged += 1
                return node_id

        row = RowWrite(
            file=RunFile.SPACE,
            params={
                "node_id": node_id,
                "space_key": space.key,
                "name": space.name,
                "space_type": space.space_type,
                "status": space.status,
                "description": space.description,
                "content_hash": space.content_hash,
                "indexer_hash": self._indexer_hash,
            },
        )
        await self._writer.write_node(conn, node_id, Surface.SPACE, row, ())
        counters.indexed += 1

        return node_id

    async def _content(
        self,
        conn: psycopg.AsyncConnection[Any],
        summary: ContentSummary,
        space_node: int,
        counters: SpaceCounters,
    ) -> None:
        surface = ContentFiles.surface(summary.kind)
        node_id = await self._writer.node(
            conn, surface, self._address.content(summary.id)
        )

        parent_node = space_node
        if summary.parent_id:
            parent_node = await self._writer.node(
                conn, Surface.PAGE, self._address.content(summary.parent_id)
            )

        await self._writer.tree(conn, node_id, parent_node)
        await self._writer.seen(conn, node_id)
        counters.seen += 1

        state = await self._writer.state(
            conn, ContentFiles.state(summary.kind), node_id
        )
        if self._unchanged(state, summary.version):
            counters.unchanged += 1
            await self._attachments(conn, summary, node_id, counters)
            await self._comments(conn, summary, node_id, counters)
            return

        body = await self._reader.body(summary)
        await self._writer.links(conn, node_id, body.links)
        row = RowWrite(
            file=ContentFiles.upsert(summary.kind), params=self._row(node_id, body)
        )
        await self._writer.write_node(
            conn,
            node_id,
            surface,
            row,
            [PushedText(aspect=Aspect.BODY, content=body.markdown)],
        )
        counters.indexed += 1
        logger.info(
            "%s %s %r: version %d indexed",
            summary.kind,
            summary.id,
            summary.title,
            body.summary.version,
        )
        await self._attachments(conn, summary, node_id, counters)
        await self._comments(conn, summary, node_id, counters)

    def _row(self, node_id: int, body: ContentBody) -> dict[str, object]:
        summary = body.summary
        row: dict[str, object] = {
            "node_id": node_id,
            "space_key": summary.space_key,
            "content_id": summary.id,
            "title": summary.title,
            "status": summary.status,
            "version": summary.version,
            "created_at": summary.created_at,
            "updated_at": summary.updated_at,
            "author": summary.author,
            "last_editor": summary.last_editor,
            "labels": list(summary.labels),
            "content_hash": body.content_hash,
            "indexer_hash": self._indexer_hash,
        }
        if summary.kind is ContentType.PAGE:
            row["ancestor_titles"] = list(summary.ancestor_titles)

        return row

    async def _comments(
        self,
        conn: psycopg.AsyncConnection[Any],
        summary: ContentSummary,
        page_node: int,
        counters: SpaceCounters,
    ) -> None:
        async for comment in self._reader.comments(summary):
            await self._comment(conn, comment, page_node, counters)

    async def _comment(
        self,
        conn: psycopg.AsyncConnection[Any],
        comment: CommentDocument,
        page_node: int,
        counters: SpaceCounters,
    ) -> None:
        node_id = await self._writer.node(
            conn, Surface.COMMENT, self._address.comment(comment.page_id, comment.id)
        )
        await self._writer.tree(conn, node_id, page_node)
        await self._writer.seen(conn, node_id)
        counters.seen += 1

        state = await self._writer.state(conn, RunFile.COMMENT_STATE, node_id)
        if self._unchanged(state, comment.version):
            counters.unchanged += 1
            return

        row = RowWrite(
            file=RunFile.COMMENT,
            params={
                "node_id": node_id,
                "space_key": comment.space_key,
                "page_id": comment.page_id,
                "comment_id": comment.id,
                "location": comment.location,
                "version": comment.version,
                "created_at": comment.created_at,
                "updated_at": comment.updated_at,
                "author": comment.author,
                "content_hash": comment.content_hash,
                "indexer_hash": self._indexer_hash,
            },
        )
        await self._writer.write_node(
            conn,
            node_id,
            Surface.COMMENT,
            row,
            [PushedText(aspect=Aspect.BODY, content=comment.markdown)],
        )
        counters.indexed += 1
        logger.info(
            "comment %s on %s: version %d indexed",
            comment.id,
            comment.page_id,
            comment.version,
        )

    async def _attachments(
        self,
        conn: psycopg.AsyncConnection[Any],
        summary: ContentSummary,
        page_node: int,
        counters: SpaceCounters,
    ) -> None:
        async for attachment in self._reader.attachments(summary):
            await self._attachment(conn, attachment, page_node, counters)

    async def _attachment(
        self,
        conn: psycopg.AsyncConnection[Any],
        attachment: AttachmentSummary,
        page_node: int,
        counters: SpaceCounters,
    ) -> None:
        node_id = await self._writer.node(
            conn,
            Surface.ATTACHMENT,
            self._address.attachment(attachment.page_id, attachment.id),
        )
        await self._writer.tree(conn, node_id, page_node)
        await self._writer.seen(conn, node_id)
        counters.seen += 1

        state = await self._writer.state(conn, RunFile.ATTACHMENT_STATE, node_id)
        if self._unchanged(state, attachment.version):
            counters.unchanged += 1
            return

        verdict = self._texts.verdict(attachment)
        if verdict is not AttachmentVerdict.TAKE:
            logger.info(
                "attachment %s %r (%s): %s, metadata only",
                attachment.id,
                attachment.title,
                attachment.media_type,
                verdict.value,
            )
            write = AttachmentWrite(attachment=attachment, content_hash="", pushed=())
            await self._write_attachment(conn, node_id, write, counters)
            return

        with tempfile.TemporaryDirectory(prefix="cfl-indexer-") as spool:
            path = Path(spool) / "attachment"
            try:
                content_hash = await self._reader.download(attachment, path)
            except AttachmentGoneError as exc:
                logger.info("attachment %s skipped: %s", attachment.id, exc)
                return

            file = DownloadedFile(
                attachment=attachment, path=path, content_hash=content_hash
            )
            try:
                pushed = await self._texts_of(conn, node_id, file, state)
            except DocumentTextError as exc:
                logger.error("attachment %s not indexed: %s", attachment.id, exc)
                counters.failed += 1
                return

        write = AttachmentWrite(
            attachment=attachment, content_hash=content_hash, pushed=pushed
        )
        await self._write_attachment(conn, node_id, write, counters)

    async def _texts_of(
        self,
        conn: psycopg.AsyncConnection[Any],
        node_id: int,
        file: DownloadedFile,
        state: NodeState | None,
    ) -> Sequence[PushedText]:
        """Тексты файла: прежние из полнотекста, если байты и параметры не менялись,
        иначе новый разбор."""
        if state is None:
            return await self._texts.extract(file.attachment, file.path)

        if state.content_hash != file.content_hash:
            return await self._texts.extract(file.attachment, file.path)

        if state.indexer_hash != self._indexer_hash:
            return await self._texts.extract(file.attachment, file.path)

        return await self._writer.pushed_texts(conn, node_id)

    async def _write_attachment(
        self,
        conn: psycopg.AsyncConnection[Any],
        node_id: int,
        write: AttachmentWrite,
        counters: SpaceCounters,
    ) -> None:
        attachment = write.attachment
        row = RowWrite(
            file=RunFile.ATTACHMENT,
            params={
                "node_id": node_id,
                "space_key": attachment.space_key,
                "page_id": attachment.page_id,
                "attachment_id": attachment.id,
                "title": attachment.title,
                "media_type": attachment.media_type,
                "file_size": attachment.file_size,
                "version": attachment.version,
                "created_at": attachment.updated_at,
                "updated_at": attachment.updated_at,
                "author": attachment.author,
                "content_hash": write.content_hash,
                "indexer_hash": self._indexer_hash,
            },
        )
        await self._writer.write_node(
            conn, node_id, Surface.ATTACHMENT, row, write.pushed
        )
        counters.indexed += 1
        logger.info(
            "attachment %s %r (%s): version %d indexed, %d text(s)",
            attachment.id,
            attachment.title,
            attachment.media_type,
            attachment.version,
            len(write.pushed),
        )


class IndexerWorker:
    """Прогон по спейсам конфига: пул к ix, эмбеддер, воркер на спейс."""

    FTS_TABLE: ClassVar[str] = "ix_fts"

    def __init__(
        self, cfg: IndexerConfig, package_dir: Path, *, reindex: bool = False
    ) -> None:
        self._cfg = cfg
        self._dir = package_dir
        self._reindex = reindex
        self._texts = AttachmentText(cfg.text_params())

    async def run(self, *, source: str = "", space: str = "") -> list[SpaceReport]:
        try:
            targets = await self._targets(source, space)
            async with IxPool.opened(self._cfg) as pool:
                sql_files = PackageSql(self._dir, self._cfg.db_schema)
                async with pool.connection() as conn:
                    await self._require_fts(conn)

                limit = asyncio.Semaphore(self._cfg.parallel_spaces)
                tasks: list[asyncio.Task[SpaceReport]] = []
                for target in targets:
                    tasks.append(
                        asyncio.create_task(self._space(pool, sql_files, target, limit))
                    )

                return list(await asyncio.gather(*tasks))
        except IxDatabaseError as exc:
            raise IndexerWorkerError(str(exc)) from exc
        except (ConfluenceReadError, DocumentTextError, IxWriteError) as exc:
            raise IndexerWorkerError(str(exc)) from exc
        except psycopg.Error as exc:
            msg = f"ix database {self._cfg.postgres.where()}: {exc}"
            raise IndexerWorkerError(msg) from exc

    async def _targets(self, source_name: str, space_key: str) -> list[SpaceTarget]:
        """Спейсы прогона: маска без glob-символов это перечисление ключей,
        маска со звёздочкой разворачивается списком спейсов сервера."""
        targets: list[SpaceTarget] = []
        for source in self._cfg.selected(source_name, space_key):
            if space_key:
                targets.append(SpaceTarget(source=source, key=space_key))
                continue

            async with SpaceReader(source.confluence) as reader:
                keys = await reader.space_keys(source.spaces)

            logger.info("source %s: %d space(s) to walk", source.name, len(keys))
            for key in keys:
                targets.append(SpaceTarget(source=source, key=key))

        return targets

    async def _space(
        self,
        pool: Any,
        sql_files: PackageSql,
        target: SpaceTarget,
        limit: asyncio.Semaphore,
    ) -> SpaceReport:
        source = target.source
        async with limit, pool.connection() as conn:
            writer = IxWriter(sql_files)
            async with SpaceReader(source.confluence) as reader:
                scope = SpaceScope(
                    address=CflAddress(source.confluence),
                    indexer_hash=IndexerHash.of(self._cfg, source),
                    reindex=self._reindex,
                )
                indexer = SpaceIndexer(reader, writer, self._texts, scope)

                return await indexer.run(conn, target.key)

    async def _require_fts(self, conn: psycopg.AsyncConnection[Any]) -> None:
        """Общий полнотекст обязан быть накачен: индексатор кладёт текст только туда,
        а его владелец — пакет ix-fts."""
        if await SchemaName.exists(conn, self._cfg.db_schema, self.FTS_TABLE):
            return

        raise IndexerWorkerError(
            f"table {self._cfg.db_schema}.{self.FTS_TABLE} is missing: apply the "
            "full-text index first: boba-ix-fts upgrade --config <config>"
        )


class Command(StrEnum):
    """Что делает запуск: накатить свою схему или обойти спейсы."""

    UPGRADE = "upgrade"
    RUN = "run"


class CliArgs(BaseModel):
    """Разобранная командная строка."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    command: Command
    config: Path
    source: str
    space: str
    reindex: bool


class Cli:
    """Команда, путь к конфигу, необязательные источник и спейс, флаг полного
    перечита; секция [ix.cfl_indexer]."""

    SECTION: ClassVar[str] = "ix.cfl_indexer"

    @classmethod
    def parse(cls, argv: Sequence[str] | None = None) -> CliArgs:
        parser = argparse.ArgumentParser(
            prog="boba-cfl-indexer",
            description=(
                "Индексатор Confluence в граф ix: схема пакета и обход спейсов "
                "со всеми индексами."
            ),
        )
        parser.add_argument(
            "command",
            type=Command,
            choices=list(Command),
            help=(
                "upgrade — накатить схему пакета в базу ix (идемпотентно, ядро "
                "должно быть уже накачено пакетом ix-core); run — обход спейсов."
            ),
        )
        parser.add_argument(
            "--config",
            required=True,
            type=Path,
            help=(
                "Путь к файлу конфига приложения (toml). Все настройки, включая "
                "профиль базы ix и endpoint Confluence, берутся из секции "
                f"[{cls.SECTION}]."
            ),
        )
        parser.add_argument(
            "--source",
            default="",
            help="Обойти только этот источник из списка sources конфига.",
        )
        parser.add_argument(
            "--space",
            default="",
            help=(
                "Обойти только этот спейс вместо списка spaces источника; при "
                "нескольких источниках в конфиге нужен --source."
            ),
        )
        parser.add_argument(
            "--reindex",
            action="store_true",
            help=(
                "Перечитать и переиндексировать всё, не отсекая по версии и хэшу "
                "параметров; на один прогон."
            ),
        )
        args = parser.parse_args(argv)

        return CliArgs(
            command=args.command,
            config=args.config,
            source=args.source,
            space=args.space,
            reindex=args.reindex,
        )


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")

    package_dir = Path(__file__).resolve().parent
    try:
        args = Cli.parse()

        if args.command is Command.UPGRADE:
            database = bind_section(args.config, Cli.SECTION, IxDatabase)
            upgrade = SchemaUpgrade(package_dir / "schema")
            report = asyncio.run(upgrade.run(database))
            logger.info("schema applied: %s", ", ".join(report.files))
            return

        cfg = bind_section(args.config, Cli.SECTION, IndexerConfig)
        worker = IndexerWorker(cfg, package_dir / "run", reindex=args.reindex)
        reports = asyncio.run(worker.run(source=args.source, space=args.space))
        failed = 0
        for report in reports:
            failed += report.failed
            logger.info(
                "done %s: seen=%d indexed=%d unchanged=%d linked=%d swept=%d failed=%d",
                report.space_key,
                report.seen,
                report.indexed,
                report.unchanged,
                report.linked,
                report.swept,
                report.failed,
            )

        if failed:
            raise SystemExit(f"{failed} attachment(s) not indexed, see the log above")
    except (
        ConfigError,
        SchemaUpgradeError,
        IndexerWorkerError,
    ) as exc:
        raise SystemExit(str(exc)) from exc


if __name__ == "__main__":
    main()
