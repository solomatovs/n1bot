"""ConfluenceParser: отпечаток спейса и страницы — sha256 сырых байт ответа.

Базы стенда тесты не трогают, но autouse-фикстура пакета асинхронная, поэтому
тесты объявлены anyio, как остальные модули.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

import pytest

from boba.cfl_indexer.confluence import ConfluenceParser, Payload
from boba.confluence.rest import ContentType

pytestmark = pytest.mark.anyio

SPACE: dict[str, Any] = {
    "key": "DEV",
    "name": "Data platform",
    "type": "global",
    "status": "current",
    "description": {"plain": {"value": "Docs of DWH"}},
}

PAGE: dict[str, Any] = {
    "id": "101",
    "type": "page",
    "status": "current",
    "title": "OrdersTable",
    "space": {"key": "DEV"},
    "version": {
        "number": 3,
        "when": "2026-02-01T00:00:00.000Z",
        "by": {"username": "u"},
    },
    "history": {
        "createdDate": "2026-01-01T00:00:00.000Z",
        "createdBy": {"username": "a"},
    },
    "ancestors": [{"id": "100", "title": "Home"}],
    "metadata": {"labels": {"results": [{"name": "dwh"}]}},
    "body": {"view": {"value": "<p>Orders</p>"}},
}


def payload_of(data: dict[str, Any], **layout: Any) -> Payload:
    raw = json.dumps(data, ensure_ascii=False, **layout).encode("utf-8")

    return Payload(data=data, raw=raw)


async def test_space_hash_is_sha256_of_the_raw_response() -> None:
    parser = ConfluenceParser("view")
    payload = payload_of(SPACE)

    space = parser.space(payload)

    assert space.content_hash == hashlib.sha256(payload.raw).hexdigest()
    assert space.name == "Data platform"
    assert parser.space(payload_of(SPACE, indent=2)).content_hash != space.content_hash


async def test_body_hash_is_sha256_of_the_raw_response() -> None:
    parser = ConfluenceParser("view")
    listed = parser.content(PAGE, ContentType.PAGE)
    payload = payload_of(PAGE)

    page = parser.body(payload, listed)

    assert page.content_hash == hashlib.sha256(payload.raw).hexdigest()
    assert page.markdown == "Orders"
    relabeled = payload_of(
        {**PAGE, "metadata": {"labels": {"results": [{"name": "etl"}]}}}
    )
    assert parser.body(relabeled, listed).content_hash != page.content_hash
