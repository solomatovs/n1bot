"""Ручной прогон индексации Confluence: IngestOps вызывается напрямую.

Конфиг прогона берётся из [tool.ingest]; запись идёт в ту же базу знаний, что
у приложения, поэтому режим и цель задаются в RunArgs осознанно.
"""

from __future__ import annotations

from typing import ClassVar

import pytest

from boba.settings import bind
from boba.tool.kb.confluence.ingest_base import ConfluenceIngestConfig
from boba.tool.kb.confluence.ingest_caller import ConfluenceIngestCaller, IngestRequest
from boba.tool.kb.confluence.ingest_payload import IngestOps

pytestmark = [pytest.mark.run, pytest.mark.anyio]


class RunArgs:
    """Аргументы прогона: правятся перед запуском."""

    MODE: ClassVar[str] = "pages"

    PAGE_IDS: ClassVar[tuple[str, ...]] = ("950276",)

    CQL: ClassVar[str] = ""

    SPACE_KEYS: ClassVar[tuple[str, ...]] = ()

    PRUNE_MISSING: ClassVar[bool] = False

    FORCE_UPDATE: ClassVar[bool] = False

    @classmethod
    def request(cls, cfg: ConfluenceIngestConfig) -> IngestRequest:
        return IngestRequest(
            op=IngestRequest.OP,
            config=ConfluenceIngestCaller.config_of(cfg),
            mode=cls.MODE,
            page_ids=cls.PAGE_IDS,
            cql=cls.CQL,
            space_keys=cls.SPACE_KEYS,
            prune_missing=cls.PRUNE_MISSING,
            force_update=cls.FORCE_UPDATE,
        )


@pytest.fixture(scope="module")
def ingest_config(raw_config):
    return bind(raw_config, path="tool.ingest", model=ConfluenceIngestConfig)


async def test_run_confluence_ingest(ingest_config, payload) -> None:
    request = RunArgs.request(ingest_config)

    answer = await IngestOps.ingest(payload.of(request))

    print(answer)
