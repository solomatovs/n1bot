"""Согласованность хоста и payload'а: имена операций, полей и секреты.

Все три проверки написаны по реальным поломкам прогона инструментов:
- операция payload'а не была включена в таблицу маршрутов main.py;
- поле уезжало под именем модели, а payload читал его alias;
- конфиг дампился как json, а SecretStr в этом режиме превращается в маску,
  и Confluence отвечал 404 на анонимный запрос.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest
from pydantic import BaseModel, SecretStr

from boba.chainlit2.agent.tools.confluence.ingest_caller import (
    ConfluenceIngestCaller,
    IngestRequest,
)
from boba.chainlit2.agent.tools.confluence.protocol import (
    ConfluenceAttachmentRequest,
    ConfluenceGrepRequest,
    ConfluencePageRequest,
    ConfluenceSearchRequest,
    ConfluenceSpacesRequest,
)
from boba.chainlit2.agent.tools.kb.caller import KbSearchRequest
from boba.chainlit2.agent.tools.pg.protocol import PgQueryRequest
from boba.chainlit2.agent.tools.web.protocol import WebFetchRequest, WebGrepRequest

_PAYLOAD = Path(__file__).resolve().parents[1] / "payloads" / "parse"

REQUEST_MODELS = [
    IngestRequest,
    KbSearchRequest,
    PgQueryRequest,
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


@pytest.fixture(scope="module")
def payload_main():
    sys.path.insert(0, str(_PAYLOAD))
    try:
        yield importlib.import_module("main").ParsePayload
    finally:
        sys.path.remove(str(_PAYLOAD))


class TestRoutes:
    """Операция без маршрута падает в рантайме как 'unknown op'."""

    def test_every_module_op_is_routed(self, payload_main) -> None:
        sys.path.insert(0, str(_PAYLOAD))
        try:
            for ops, module_name, class_name in payload_main.ROUTES:
                module = importlib.import_module(module_name)
                declared = set(getattr(module, class_name).OPS)
                assert declared == set(ops), (
                    f"{module_name}.{class_name}.OPS={sorted(declared)} "
                    f"не совпадает с маршрутом {sorted(ops)}"
                )
        finally:
            sys.path.remove(str(_PAYLOAD))

    def test_every_payload_module_is_reachable(self, payload_main) -> None:
        routed = set()
        for _, module_name, _ in payload_main.ROUTES:
            routed.add(module_name)
        for path in _PAYLOAD.glob("*.py"):
            name = path.stem
            if name == "main":
                continue
            if "OPS: ClassVar" not in path.read_text():
                continue
            assert name in routed, (
                f"{name}.py объявляет OPS, но его нет в ParsePayload.ROUTES"
            )


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
