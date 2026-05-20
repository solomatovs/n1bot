"""Pytest-фикстуры пакета boba-html (infra/format)."""

from __future__ import annotations

from collections.abc import Callable
from io import BytesIO

import pytest

from boba.indexing import Metadata, RawDocument, SourceId


@pytest.fixture
def make_raw_doc() -> Callable[..., RawDocument]:
    """Фабрика `RawDocument` поверх in-memory BytesIO.

    Сигнатура надмножество того, что нужно reader-тестам и
    format-plan-тестам: `html` обязателен, `source_id`/`metadata`
    с дефолтами. Раньше `_doc(...)` дублировался в test_html_reader.py
    и test_format_plan.py — теперь один источник правды.
    """

    def _factory(
        html: str,
        *,
        source_id: str = "fs:/x",
        metadata: Metadata | None = None,
    ) -> RawDocument:
        return RawDocument(
            handle=BytesIO(html.encode("utf-8")),
            source_id=SourceId(source_id),
            metadata=metadata or Metadata.empty(),
        )

    return _factory
