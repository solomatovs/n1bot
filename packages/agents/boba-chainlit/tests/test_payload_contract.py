"""Согласованность хоста и payload'а: точки входа, alias'ы полей, маска SecretStr."""

from __future__ import annotations

import importlib
import subprocess
import sys
from pathlib import Path
from typing import ClassVar, Protocol

import pytest
from pydantic import BaseModel, SecretStr

from boba.tool.ch import ChCaller, ChQueryRequest
from boba.tool.chart.caller import ChartCaller
from boba.tool.doc.engine import DocEngine
from boba.tool.doc.liteparse import LiteParseCaller
from boba.tool.kb.caller import KbCaller, KbSearchRequest
from boba.tool.kb.confluence.caller import ConfluenceCaller
from boba.tool.kb.confluence.ingest_caller import (
    ConfluenceIngestCaller,
    IngestRequest,
)
from boba.tool.kb.confluence.protocol import (
    ConfluenceAttachmentRequest,
    ConfluenceGrepRequest,
    ConfluencePageRequest,
    ConfluenceSearchRequest,
    ConfluenceSpacesRequest,
)
from boba.tool.kb.html.caller import HtmlCaller
from boba.tool.pg.caller import PgCaller
from boba.tool.pg.protocol import PgQueryRequest
from boba.web.caller import WebCaller
from boba.web.protocol import WebFetchRequest, WebGrepRequest


class PayloadCaller(Protocol):
    """Контракт caller'а: точка входа payload'а как аргументы `python3 -m`."""

    ENTRY: ClassVar[tuple[str, ...]]


CALLERS: list[type[PayloadCaller]] = [
    ChartCaller,
    DocEngine,
    LiteParseCaller,
    HtmlCaller,
    KbCaller,
    ConfluenceCaller,
    ConfluenceIngestCaller,
    PgCaller,
    ChCaller,
    WebCaller,
]

REQUEST_MODELS = [
    IngestRequest,
    KbSearchRequest,
    PgQueryRequest,
    ChQueryRequest,
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

    @pytest.mark.parametrize("caller", CALLERS)
    def test_entry_is_an_importable_module(self, caller: type[PayloadCaller]) -> None:
        entry = caller.ENTRY
        assert entry[:2] == ("python3", "-m"), (
            f"{caller.__name__}.ENTRY={entry} — payload запускается как модуль"
        )
        importlib.import_module(entry[2])

    @pytest.mark.parametrize("caller", CALLERS)
    def test_entry_module_is_runnable(self, caller: type[PayloadCaller]) -> None:
        """У модуля должен быть __main__: иначе `python3 -m` ничего не сделает."""
        module = importlib.import_module(caller.ENTRY[2])
        assert module.__file__ is not None
        source = Path(module.__file__).read_text()
        assert '__name__ == "__main__"' in source, (
            f"{caller.ENTRY[2]} не запускается как `python3 -m`"
        )

    def test_unknown_op_is_reported(self) -> None:
        """Незнакомая операция обязана падать, а не молчать."""
        result = subprocess.run(
            [sys.executable, "-m", "boba.tool.chart.payload"],
            input='{"op": "нет-такой-op"}',
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode != 0
        assert "unknown chart op" in result.stderr


class TestFieldNames:
    """call_json дампит по именам полей: alias до payload'а не доедет."""

    @pytest.mark.parametrize("model", REQUEST_MODELS)
    def test_request_fields_have_no_alias(self, model: type[BaseModel]) -> None:
        aliased = []
        for name, field in model.model_fields.items():
            if field.alias is not None:
                aliased.append(f"{name} -> {field.alias}")
        assert not aliased, (
            f"{model.__name__}: alias у полей {aliased} — "
            "SandboxCaller.call_json сериализует по именам полей"
        )


class TestSecrets:
    """Конфиг едет в песочницу через stdin: секреты должны быть настоящими."""

    def test_config_of_reveals_secret(self) -> None:
        class Auth(BaseModel):
            method: str
            token: SecretStr

        class Cfg(BaseModel):
            auth: Auth
            items: list[Auth]

        auth = Auth(method="bearer", token=SecretStr("s3cret"))
        revealed = ConfluenceIngestCaller.config_of(Cfg(auth=auth, items=[auth]))
        assert revealed["auth"]["token"] == "s3cret"
        assert revealed["items"][0]["token"] == "s3cret"
