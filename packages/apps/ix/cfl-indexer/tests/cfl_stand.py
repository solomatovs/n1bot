"""Помощники стенда индексатора Confluence: конфиг на заглушке, прогон по спейсу и
прогон общих индексаторов.

Индексатор Confluence кладёт в ix_fts только добытый текст; title, path, words, card,
триграммы и векторы выводят из объявлений общие индексаторы. Тест гоняет их следом,
поэтому проверяет цепочку целиком — и заодно то, что новое происхождение подхватывается
ими без правок.
"""

from __future__ import annotations

from pathlib import Path

from boba.cfl_indexer import worker as indexer
from boba.cfl_indexer.confluence import SpaceSelector
from boba.cfl_indexer.worker import (
    ConfluenceSource,
    Indexer,
    IndexerConfig,
    Report,
    SpaceSelection,
)
from boba.confluence.rest import ConfluenceConnection, SpaceType
from boba.doc.config import DisabledOcrConfig, DocSection
from boba.ix_core.aspects import AspectClass
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
from boba.stand.ix import IxStand
from boba.transport.http.profile import HttpConnection, UrlScheme

__all__ = ["PACKAGE_DIR", "SharedIndexers", "StubIndexer"]

PACKAGE_DIR = Path(indexer.__file__).resolve().parent


class StubIndexer:
    """Индексатор, собранный на заглушке: конфиг из стенда, прогон по спейсу."""

    def __init__(self, stand: IxStand, port: int) -> None:
        self._stand = stand
        self._port = port

    def config(self, *spaces: str) -> IndexerConfig:
        profile = HttpConnection(
            scheme=UrlScheme.HTTP, host="127.0.0.1", port=self._port
        )
        database = self._stand.ix_database

        return IndexerConfig(
            db_schema=database.db_schema,
            postgres=database.postgres,
            sources=[
                ConfluenceSource(
                    name="stub",
                    confluence=ConfluenceConnection(profile=profile),
                    spaces=SpaceSelector(
                        masks=list(spaces), type=SpaceType.GLOBAL, archived=True
                    ),
                )
            ],
            parallel_spaces=1,
            list_limit=50,
            doc=DocSection(
                spool_memory_limit=32 << 20,
                text_encodings=("utf-8",),
                ocr=DisabledOcrConfig(provider="off"),
            ),
        )

    async def run(self, *spaces: str) -> list[Report]:
        """Прогон в потоке: процессы спейсов ходят в заглушку, которую обслуживает
        event loop теста, и блокировать его ожиданием нельзя."""
        cfg = self.config(*spaces)

        return await Indexer(cfg, PACKAGE_DIR / "run", self._stand.krb).run(
            SpaceSelection()
        )


class SharedIndexers:
    """Общие индексаторы поверх той же базы: выводят аспекты из объявлений и
    раскладывают их по ix_trgm, ix_fts и ix_emb_e5_1024."""

    WEIGHTS = {
        "title": FtsWeight.A,
        "words": FtsWeight.A,
        "path": FtsWeight.B,
        "labels": FtsWeight.B,
        "card": FtsWeight.B,
        "body": FtsWeight.C,
        "ocr": FtsWeight.C,
    }

    def __init__(self, stand: IxStand) -> None:
        self._stand = stand

    async def text(self) -> None:
        """Триграммы и полнотекст: без модели, поэтому быстро."""
        database = self._stand.ix_database
        common = {
            "db_schema": database.db_schema,
            "postgres": database.postgres,
        }

        trgm_cfg = TrgmConfig(**common, classes=[AspectClass.IDENT, AspectClass.WORDS])
        await TrgmWorker(trgm_cfg, Path(trgm.__file__).resolve().parent / "run").run()

        fts_cfg = FtsConfig(
            **common,
            classes=[AspectClass.IDENT, AspectClass.WORDS, AspectClass.DESCRIPTION],
            weights=self.WEIGHTS,
        )
        await FtsWorker(fts_cfg, Path(fts.__file__).resolve().parent / "run").run()

    async def vectors(self) -> None:
        """Векторы: поднимает модель, поэтому зовётся только там, где проверяется."""
        database = self._stand.ix_database
        cfg = VectorConfig(
            db_schema=database.db_schema,
            postgres=database.postgres,
            classes=[AspectClass.DESCRIPTION],
            cache_dir=self._stand.embedding_cache_dir,
        )
        await VectorWorker(cfg, Path(vector.__file__).resolve().parent / "run").run()
