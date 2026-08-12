"""Узел индексации внутри песочницы: там его и запускает инструмент.

Прогон целиком требует Confluence и postgres, поэтому проверяется граница —
что модуль догружается в песочнице со всеми зависимостями конвейера и
отвечает по контракту, а не падает импортом.
"""

from __future__ import annotations

from typing import Any

import pytest
from conftest import StageTestRegistry, needs_sandbox, needs_userns, sandbox_profile

from boba.tool.kb.confluence.ingest_base import ConfluenceIngestConfig
from boba.tool.kb.confluence.ingest_caller import ConfluenceIngestCaller
from boba.tool.kb.confluence.ingest_protocol import IngestMode
from boba.tool.kb.confluence.ingest_stages import ConfluenceIngestStages
from boba.toolkit.launcher import PayloadFailureError

_UNREACHABLE_KINDS = frozenset({"database_unavailable", "ingest_request_failed"})
"""Сети у профиля нет: прогон обязан упереться в недоступность, а не в импорт."""


def _config() -> ConfluenceIngestConfig:
    return ConfluenceIngestConfig.model_validate(
        {
            "connection": {
                "host": "127.0.0.1",
                "port": 1,
                "user": "kb",
                "dbname": "kb",
            },
            "tables": {},
            "embedding": {
                "model": "intfloat/multilingual-e5-large",
                "cache_dir": "/var/cache/fastembed",
                "dim": 1024,
                "batch_size": 4,
            },
            "confluence": {"base_url": "http://127.0.0.1:1", "timeout_sec": 1},
            "tessdata_path": "/usr/share/tessdata",
            "page_workers": 1,
        }
    )


def _caller() -> ConfluenceIngestCaller:
    nodes = ConfluenceIngestStages.of(_config())
    launchers = StageTestRegistry.launchers(nodes, sandbox_profile())

    return ConfluenceIngestCaller("ingest", launchers)


def _ingest() -> dict[str, Any]:
    return _caller().ingest(
        mode=IngestMode.PAGES,
        page_ids=["1"],
        prune_missing=False,
        force_update=False,
        ocr_enabled=False,
        num_workers=1,
        ocr_language="rus+eng",
    )


@needs_sandbox
@needs_userns
def test_payload_loads_and_reports_unreachable_source() -> None:
    """Важно, что ответ — про недоступность источника, а не про импорт модулей."""
    with pytest.raises(PayloadFailureError) as failure:
        _ingest()

    assert failure.value.kind in _UNREACHABLE_KINDS

    reason = str(failure.value)
    assert "ModuleNotFoundError" not in reason
    assert "ImportError" not in reason
