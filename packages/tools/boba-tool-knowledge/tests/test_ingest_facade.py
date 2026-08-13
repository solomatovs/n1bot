"""Ingest-фасады: настройки OCR из вызова LLM доезжают до запроса узла."""

from __future__ import annotations

from typing import Any

import pytest
from conftest import RecordingLauncher

from boba.tool.kb.confluence.ingest_base import ConfluenceIngestConfig
from boba.tool.kb.confluence.ingest_protocol import (
    ConfluenceIngestRequest,
    IngestAnswer,
)
from boba.tool.kb.confluence.ingest_stages import ConfluenceIngestStages
from boba.tool.kb.confluence.ingest_tools import ConfluenceIngestTools
from boba.tool.kb.confluence.protocol import (
    ConfluenceAttachmentRequest,
    ConfluenceNode,
)
from boba.toolkit.workflow import EmptyTrailer


def _config() -> ConfluenceIngestConfig:
    return ConfluenceIngestConfig.model_validate(
        {
            "connection": {"host": "db", "user": "kb", "dbname": "kb"},
            "tables": {},
            "embedding": {"model": "intfloat/e5", "dim": 8, "batch_size": 4},
            "confluence": {"base_url": "https://confl.example"},
            "tessdata_path": "/usr/share/tessdata",
            "page_workers": 1,
        }
    )


@pytest.fixture
def launcher() -> RecordingLauncher:
    nodes = ConfluenceIngestStages.of(_config())
    trailers = {
        ConfluenceNode.INGEST.value: IngestAnswer(stats={}),
        ConfluenceNode.ATTACHMENT.value: EmptyTrailer(),
    }

    return RecordingLauncher(nodes, trailers)


def _tools(launcher: RecordingLauncher) -> dict[str, Any]:
    built = ConfluenceIngestTools(_config(), lambda _tool: launcher).build()

    return {tool.name: tool for tool in built}


class TestIngestOcrParams:
    """Настройки парсера складываются в конфиг прогона обогатителем узла."""

    @staticmethod
    def _ingest_request(launcher: RecordingLauncher) -> ConfluenceIngestRequest:
        request = launcher.requests[0]
        assert isinstance(request, ConfluenceIngestRequest)
        return request

    def test_llm_params_override_config(self, launcher: RecordingLauncher) -> None:
        _tools(launcher)["confluence_ingest"].invoke(
            {
                "mode": "pages",
                "page_ids": ["1"],
                "ocr_enabled": True,
                "num_workers": 3,
                "ocr_language": "rus",
            }
        )

        cfg = self._ingest_request(launcher).config
        assert cfg.ocr_enabled is True
        assert cfg.num_workers == 3
        assert cfg.ocr_language == "rus"

    def test_facade_defaults_apply(self, launcher: RecordingLauncher) -> None:
        _tools(launcher)["confluence_ingest"].invoke(
            {"mode": "cql", "cql": "space = DQ"}
        )

        cfg = self._ingest_request(launcher).config
        assert cfg.ocr_enabled is False
        assert cfg.num_workers == 1
        assert cfg.ocr_language == "rus+eng"

    def test_parser_knobs_do_not_leak_into_the_request(
        self,
        launcher: RecordingLauncher,
    ) -> None:
        """Ручки парсера живут в конфиге прогона, отдельными полями их нет."""
        _tools(launcher)["confluence_ingest"].invoke(
            {"mode": "spaces", "space_keys": ["DQ"]}
        )

        request = self._ingest_request(launcher)
        assert "ocr_enabled" not in request.model_dump()

    def test_attachment_carries_overrides(self, launcher: RecordingLauncher) -> None:
        _tools(launcher)["confluence_attachment"].invoke(
            {
                "page_id": "1",
                "filename": "report.pdf",
                "ocr_enabled": True,
                "num_workers": 2,
                "ocr_language": "eng",
            }
        )

        request = launcher.requests[0]
        assert isinstance(request, ConfluenceAttachmentRequest)
        assert request.ocr_enabled is True
        assert request.num_workers == 2
        assert request.ocr_language == "eng"
        assert request.tessdata_path == "/usr/share/tessdata"

    @pytest.mark.parametrize(
        "name",
        [
            "confluence_ingest",
            "confluence_attachment",
        ],
    )
    def test_ocr_controls_are_optional_with_defaults(
        self,
        launcher: RecordingLauncher,
        name: str,
    ) -> None:
        schema = _tools(launcher)[name].get_input_schema().model_json_schema()
        props = schema["properties"]
        assert props["ocr_enabled"]["default"] is False
        assert props["num_workers"]["default"] == 1
        assert props["num_workers"]["maximum"] == 4
        assert props["ocr_language"]["default"] == "rus+eng"
        for control in ("ocr_enabled", "num_workers", "ocr_language"):
            assert control not in schema.get("required", [])
