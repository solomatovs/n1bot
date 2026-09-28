"""Индексы ix для стендов: схемы стека Confluence, страницы через хранилище
cfl-indexer без обхода Confluence и общие индексаторы trgm/fts/vector одним
проходом. Тесты cfl-indexer прогоняют индексаторы после обхода заглушки,
UI-стенд — после записи своих страниц.

Ошибки наружу — ошибки пакетов ix и драйвера postgres как есть: стенд падает
на месте.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar

from boba.cfl_indexer import worker as indexer
from boba.cfl_indexer.confluence import Content
from boba.cfl_indexer.store import Aspect, IxStore, Surface
from boba.confluence.rest import ContentType
from boba.db.postgres import AsyncPostgresPool
from boba.ix_core.aspects import AspectClass
from boba.ix_core.database import IxDatabase
from boba.ix_fts import worker as fts
from boba.ix_fts.worker import FtsWeight
from boba.ix_fts.worker import IndexerWorker as FtsWorker
from boba.ix_fts.worker import WorkerConfig as FtsConfig
from boba.ix_trgm import worker as trgm
from boba.ix_trgm.worker import IndexerWorker as TrgmWorker
from boba.ix_trgm.worker import WorkerConfig as TrgmConfig
from boba.ix_vector import worker as vector
from boba.ix_vector.worker import VectorWorker
from boba.ix_vector.worker import WorkerConfig as VectorConfig
from boba.llm.fastembed import FastembedProvider
from boba.llm.providers import EmbeddingModelConfig, LlmProviders, LlmProviderTypes
from boba.stand.ix import IxSchemas

__all__ = ["IxPage", "IxPages", "SharedIndexers", "StandIxStack"]


class StandIxStack:
    """Пакеты ix стека Confluence: каталоги схем в порядке наката и каталог
    run cfl-indexer. Тела аспектов cfl читают ix_fts, поэтому схема
    индексаторов идёт раньше схемы cfl-indexer."""

    SCHEMA: ClassVar[str] = "schema"
    RUN: ClassVar[str] = "run"

    def schema_dirs(self) -> list[Path]:
        dirs: list[Path] = []
        for module in (fts, trgm, vector, indexer):
            dirs.append(self._package_dir(module.__file__) / self.SCHEMA)

        return dirs

    def indexer_run_dir(self) -> Path:
        return self._package_dir(indexer.__file__) / self.RUN

    async def apply(self, database: IxDatabase) -> None:
        await IxSchemas(database).apply(self.schema_dirs())

    def _package_dir(self, module_file: str | None) -> Path:
        if module_file is None:
            msg = "ix stack: a package module has no __file__ to find its schema"
            raise ValueError(msg)

        return Path(module_file).resolve().parent


@dataclass(frozen=True)
class IxPage:
    """Страница Confluence стенда: id содержимого, заголовок и тело markdown."""

    content_id: str
    title: str
    body: str


class IxPages:
    """Запись страниц в ix стенда хранилищем cfl-indexer: node, строка cfl_page
    и тело в ix_fts, как их пишет обход спейса. Адрес — вымышленный узел
    Confluence стенда, по нему собирается ссылка страницы."""

    SPACE: ClassVar[str] = "STAND"
    AUTHOR: ClassVar[str] = "stand"
    INDEXER_HASH: ClassVar[str] = "stand"
    ORIGIN: ClassVar[dict[str, object]] = {
        "scheme": "https",
        "host": "confluence.stand",
        "port": 443,
    }

    def __init__(self, database: IxDatabase) -> None:
        self._database = database
        self._stack = StandIxStack()

    async def write(self, pages: Sequence[IxPage]) -> list[int]:
        """Страницы по порядку; результат — id их node в том же порядке."""
        written: list[int] = []
        async with await AsyncPostgresPool.dedicated(self._database.postgres) as conn:
            store = IxStore(
                self._stack.indexer_run_dir(), self._database.db_schema, conn
            )
            for page in pages:
                address = {**self.ORIGIN, "content": page.content_id}
                node_id = await store.upsert_node(Surface.PAGE, address)
                async with store.transaction():
                    await store.write_content(
                        node_id, self._content(page), self.INDEXER_HASH
                    )
                    await store.push_text(node_id, Surface.PAGE, Aspect.BODY, page.body)

                written.append(node_id)

        return written

    def _content(self, page: IxPage) -> Content:
        now = datetime.now(UTC)
        return Content(
            id=page.content_id,
            kind=ContentType.PAGE,
            space_key=self.SPACE,
            title=page.title,
            status="current",
            version=1,
            parent_id="",
            ancestor_titles=(),
            labels=(),
            created_at=now,
            updated_at=now,
            author=self.AUTHOR,
            last_editor=self.AUTHOR,
        )


class SharedIndexers:
    """Общие индексаторы поверх базы ix: выводят аспекты из объявлений и
    раскладывают их по ix_trgm, ix_fts и ix_emb_e5_1024 одним проходом."""

    WEIGHTS: ClassVar[dict[str, FtsWeight]] = {
        "title": FtsWeight.A,
        "words": FtsWeight.A,
        "path": FtsWeight.B,
        "labels": FtsWeight.B,
        "card": FtsWeight.B,
        "body": FtsWeight.C,
        "ocr": FtsWeight.C,
    }
    RUN: ClassVar[str] = "run"

    def __init__(self, database: IxDatabase, cache_dir: str) -> None:
        self._database = database
        self._cache_dir = cache_dir

    async def text(self) -> None:
        """Триграммы и полнотекст: без модели, поэтому быстро."""
        database = self._database
        trgm_cfg = TrgmConfig(
            db_schema=database.db_schema,
            postgres=database.postgres,
            classes=[AspectClass.IDENT, AspectClass.WORDS],
        )
        await TrgmWorker(trgm_cfg, self._run_dir(trgm.__file__)).run()

        fts_cfg = FtsConfig(
            db_schema=database.db_schema,
            postgres=database.postgres,
            classes=[AspectClass.IDENT, AspectClass.WORDS, AspectClass.DESCRIPTION],
            weights=self.WEIGHTS,
        )
        await FtsWorker(fts_cfg, self._run_dir(fts.__file__)).run()

    async def vectors(self) -> None:
        """Векторы: поднимает модель, поэтому зовётся только там, где проверяется."""
        cfg = VectorConfig(
            db_schema=self._database.db_schema,
            postgres=self._database.postgres,
            classes=[AspectClass.DESCRIPTION],
            tokenizer_dir=self._cache_dir,
            chunk_tokens=400,
            chunk_overlap=50,
            batch=64,
            embedding=EmbeddingModelConfig(
                provider=FastembedProvider(kind="fastembed", cache_dir=self._cache_dir),
                model="intfloat/multilingual-e5-large",
                dim=1024,
                batch_size=8,
                progress_every=64,
            ),
        )
        providers = LlmProviders(LlmProviderTypes.installed())
        try:
            embedder = providers.embedding(cfg.embedding)
            await VectorWorker(cfg, embedder, self._run_dir(vector.__file__)).run()
        finally:
            await providers.aclose()

    def _run_dir(self, module_file: str | None) -> Path:
        if module_file is None:
            msg = "ix indexers: a worker module has no __file__ to find its run dir"
            raise ValueError(msg)

        return Path(module_file).resolve().parent / self.RUN
