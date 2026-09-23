"""Обход живого спейса Confluence стенда: граф заполняется, повторный прогон пуст.

Ошибки стенда: StandError/IxStandError — секций нет, модуль пропускается.
"""

from __future__ import annotations

import pytest
from cfl_stand import PACKAGE_DIR

from boba.cfl_indexer.confluence import SpaceSelector
from boba.cfl_indexer.worker import (
    ConfluenceSource,
    Indexer,
    IndexerConfig,
    SpaceSelection,
)
from boba.confluence.rest import ConfluenceConnection, SpaceType
from boba.doc.config import DisabledOcrConfig, DocSection
from boba.stand.ix import IxStand, IxStandDatabase
from boba.stand.site import Stand
from boba.transport.http.connection import BearerAuth, HttpConnection

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

STAND = Stand.required()
SPACE = "IPKD"
"""Самый маленький спейс стенда: одна страница."""


def _config(ix_stand: IxStand) -> IndexerConfig:
    connection = HttpConnection(
        host=STAND.confluence_host,
        port=STAND.confluence_port,
        auth=BearerAuth(method="bearer", token=STAND.confluence_token),
        ssl_verify=False,
    )
    database = ix_stand.ix_database

    return IndexerConfig(
        db_schema=database.db_schema,
        postgres=database.postgres,
        sources=[
            ConfluenceSource(
                name="stand",
                confluence=ConfluenceConnection(connection=connection),
                spaces=SpaceSelector(
                    masks=[SPACE], type=SpaceType.GLOBAL, archived=True
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


class TestLiveSpace:
    async def test_space_indexes_and_settles(
        self, ix_stand: IxStand, ix_database: IxStandDatabase
    ) -> None:
        if not STAND.confluence_token.get_secret_value():
            pytest.skip("в конфиге стенда нет токена confluence")

        cfg = _config(ix_stand)

        first = (
            await Indexer(cfg, PACKAGE_DIR / "run", ix_stand.krb).run(SpaceSelection())
        )[0]
        assert first.seen >= 2
        assert first.indexed == first.seen

        second = (
            await Indexer(cfg, PACKAGE_DIR / "run", ix_stand.krb).run(SpaceSelection())
        )[0]
        assert second.indexed == 0
        assert second.unchanged == second.seen
