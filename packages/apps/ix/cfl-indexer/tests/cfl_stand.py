"""Помощники стенда индексатора Confluence: конфиг на заглушке и прогон по спейсу."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from boba.cfl_indexer import worker as indexer
from boba.cfl_indexer.confluence import SpaceSelector
from boba.cfl_indexer.worker import (
    ConfluenceSource,
    IndexerConfig,
    IndexerWorker,
    SpaceReport,
)
from boba.confluence.rest import ConfluenceConnection, SpaceType
from boba.pg_idx_fts.worker import FtsWeight
from boba.stand.ix import IxStand
from boba.text.document import LiteParseParams
from boba.transport.http.profile import HttpConnection, UrlScheme

__all__ = ["PACKAGE_DIR", "WEIGHTS", "StubIndexer"]

PACKAGE_DIR = Path(indexer.__file__).resolve().parent

WEIGHTS: Mapping[str, FtsWeight] = {
    "title": FtsWeight.A,
    "words": FtsWeight.A,
    "path": FtsWeight.B,
    "labels": FtsWeight.B,
    "card": FtsWeight.B,
    "body": FtsWeight.C,
    "ocr": FtsWeight.C,
}


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
            krb=database.krb,
            cache_dir=self._stand.embedding_cache_dir,
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
            parser=LiteParseParams(),
            weights=WEIGHTS,
        )

    async def run(self, *spaces: str) -> list[SpaceReport]:
        cfg = self.config(*spaces)

        return await IndexerWorker(cfg, PACKAGE_DIR / "run").run()
