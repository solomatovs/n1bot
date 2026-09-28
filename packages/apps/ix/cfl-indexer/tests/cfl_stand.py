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
from boba.stand.ix import IxStand
from boba.transport.http.connection import HttpConnection, UrlScheme

__all__ = ["PACKAGE_DIR", "StubIndexer"]

PACKAGE_DIR = Path(indexer.__file__).resolve().parent


class StubIndexer:
    """Индексатор, собранный на заглушке: конфиг из стенда, прогон по спейсу."""

    def __init__(self, stand: IxStand, port: int) -> None:
        self._stand = stand
        self._port = port

    def config(self, *spaces: str) -> IndexerConfig:
        connection = HttpConnection(
            scheme=UrlScheme.HTTP, host="127.0.0.1", port=self._port
        )
        database = self._stand.ix_database

        return IndexerConfig(
            db_schema=database.db_schema,
            postgres=database.postgres,
            sources=[
                ConfluenceSource(
                    name="stub",
                    confluence=ConfluenceConnection(connection=connection),
                    spaces=SpaceSelector(
                        masks=list(spaces), type=SpaceType.GLOBAL, archived=True
                    ),
                )
            ],
            parallel_spaces=1,
            list_limit=50,
            doc=DocSection(
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
