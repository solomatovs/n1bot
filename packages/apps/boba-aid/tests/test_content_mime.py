"""Правила распознавания MIME и единый выбор обработчика для ленты и Canvas."""

import pytest

from boba.aid.canvas.panel import AudioViewer, ImageViewer, PdfViewer
from boba.aid.rendering.content import handler_for
from boba.aid.rendering.handlers.files import FileHandler, PdfHandler
from boba.aid.rendering.handlers.text import CodeHandler, CsvHandler, JsonHandler
from boba.aid.rendering.mime import Mime, SupportedMime

pytestmark = pytest.mark.anyio


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("Application/JSON; charset=UTF-8", JsonHandler),
        (' text/csv ; charset="utf-8"', CsvHandler),
        ("application/json-seq", FileHandler),
        ("text/x-unknown", FileHandler),
        ("image/x-unknown", FileHandler),
        ("audio/x-unknown", FileHandler),
        ("video/x-unknown", FileHandler),
        ("text/html;profile=mcp-app", FileHandler),
        ('TEXT/HTML; charset=utf-8; PROFILE="mcp-app"', FileHandler),
        ('application/json; profile="https://example.org/schema"', JsonHandler),
        ('application/pdf; profile="archive"', PdfHandler),
        ('text/html; profile="https://example.org/document"', CodeHandler),
        ("application/pdf; profile=mcp-app", PdfHandler),
        ("application/json; profile=mcp-app", JsonHandler),
        ("text/html; profile=mcp-app-extra", CodeHandler),
        ("text/x-unknown; profile=mcp-app", FileHandler),
    ],
)
async def test_mime_dispatch(value: str, expected: type) -> None:
    assert type(handler_for(Mime.parse(value).supported)) is expected


async def test_mime_parameters_are_preserved() -> None:
    value = 'text/html; profile="mcp-app"; charset=utf-8'
    mime = Mime.parse(value)
    assert mime.value == value
    assert mime.parameters == (("profile", "mcp-app"), ("charset", "utf-8"))
    assert mime.is_mcp_app
    assert mime.supported is None


async def test_unknown_profile_is_preserved_with_the_base_format() -> None:
    value = 'application/json; profile="https://example.org/schema"; charset=latin-1'
    mime = Mime.parse(value)
    assert mime.supported is SupportedMime.JSON
    assert not mime.is_mcp_app
    assert mime.value == value
    assert mime.parameters == (
        ("profile", "https://example.org/schema"),
        ("charset", "latin-1"),
    )
    stored = mime.utf8()
    assert stored.supported is SupportedMime.JSON
    assert stored.parameters == (
        ("profile", "https://example.org/schema"),
        ("charset", "utf-8"),
    )


async def test_every_declared_mime_has_a_concrete_handler() -> None:
    for mime in SupportedMime:
        assert type(handler_for(mime)) is not FileHandler, mime


async def test_canvas_uses_the_same_exact_formats() -> None:
    assert ImageViewer().handles("IMAGE/PNG; charset=utf-8")
    assert not ImageViewer().handles("image/x-unknown")
    assert not AudioViewer().handles("audio/x-unknown")
    assert PdfViewer().handles("APPLICATION/PDF")
    assert PdfViewer().handles('APPLICATION/PDF; profile="archive"')
    assert not PdfViewer().handles("application/pdf-extra")
