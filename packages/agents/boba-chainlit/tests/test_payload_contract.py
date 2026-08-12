"""Согласованность хоста и payload'а: точки входа узлов, имена полей, секреты."""

from __future__ import annotations

import importlib
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import BaseModel

from boba.tool.ch.protocol import ChInsertRequest, ChQueryRequest
from boba.tool.ch.stages import ChQueryNode
from boba.tool.chart.caller import ChartCaller
from boba.tool.doc.engine import DocEngine
from boba.tool.doc.liteparse import LiteParseCaller
from boba.tool.kb.confluence.ingest_protocol import ConfluenceIngestRequest
from boba.tool.kb.confluence.ingest_stages import ConfluenceIngestStages
from boba.tool.kb.confluence.protocol import (
    ConfluenceAttachmentRequest,
    ConfluenceGrepRequest,
    ConfluencePageRequest,
    ConfluenceSearchRequest,
    ConfluenceSpacesRequest,
)
from boba.tool.kb.confluence.stages import ConfluenceStages
from boba.tool.kb.html.stages import HtmlStages
from boba.tool.kb.protocol import KbSearchRequest
from boba.tool.kb.stages import KbStages
from boba.tool.pg.protocol import PgCopyRequest, PgQueryRequest
from boba.tool.pg.stages import PgQueryNode
from boba.tool.shell.protocol import BashArgs, BashStage
from boba.toolkit.channels import Channel
from boba.web.protocol import WebFetchRequest, WebGrepRequest, WebNodes

ENTRIES: list[tuple[str, ...]] = [
    BashStage.ENTRY,
    ChartCaller.ENTRY,
    DocEngine.ENTRY,
    LiteParseCaller.ENTRY,
    HtmlStages.ENTRY,
    KbStages.ENTRY,
    ConfluenceStages.ENTRY,
    ConfluenceIngestStages.INGEST_ENTRY,
    PgQueryNode.ENTRY,
    ChQueryNode.ENTRY,
    WebNodes.ENTRY,
]

REQUEST_MODELS: list[type[BaseModel]] = [
    BashArgs,
    ConfluenceIngestRequest,
    KbSearchRequest,
    PgQueryRequest,
    PgCopyRequest,
    ChQueryRequest,
    ChInsertRequest,
    WebFetchRequest,
    WebGrepRequest,
    ConfluencePageRequest,
    ConfluenceGrepRequest,
    ConfluenceSearchRequest,
    ConfluenceSpacesRequest,
    ConfluenceAttachmentRequest,
]


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    pass


class TestEntryPoints:
    """Точка входа — свойство кода инструмента, а не конфига."""

    @pytest.mark.parametrize("entry", ENTRIES)
    def test_entry_is_an_importable_module(self, entry: tuple[str, ...]) -> None:
        assert entry[:2] == ("python3", "-m"), (
            f"{entry} — payload запускается как модуль"
        )
        importlib.import_module(entry[2])

    @pytest.mark.parametrize("entry", ENTRIES)
    def test_entry_module_is_runnable(self, entry: tuple[str, ...]) -> None:
        """У модуля должен быть __main__: иначе `python3 -m` ничего не сделает."""
        module = importlib.import_module(entry[2])
        assert module.__file__ is not None
        source = Path(module.__file__).read_text()
        assert '__name__ == "__main__"' in source, (
            f"{entry[2]} не запускается как `python3 -m`"
        )

    def test_payload_without_channels_refuses_to_start(self) -> None:
        """Запрос приезжает каналом: без каналов payload падает, а не читает stdin."""
        result = subprocess.run(
            [sys.executable, "-m", "boba.tool.chart.payload"],
            input='{"op": "validate_figure", "spec": "{}"}',
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode != 0
        assert Channel.TOOL_ARGS.env_name in result.stderr


class TestFieldNames:
    """Запрос уезжает в tool_args по именам полей: alias до payload'а не доедет."""

    @pytest.mark.parametrize("model", REQUEST_MODELS)
    def test_request_fields_have_no_alias(self, model: type[BaseModel]) -> None:
        aliased = []
        for name, field in model.model_fields.items():
            if field.alias is not None:
                aliased.append(f"{name} -> {field.alias}")
        assert not aliased, (
            f"{model.__name__}: alias у полей {aliased} — "
            "запрос сериализуется по именам полей"
        )
