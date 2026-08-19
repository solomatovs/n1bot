"""Ingest-функции: настройки OCR из вызова LLM доезжают до конфига прогона."""

from __future__ import annotations

from typing import Any, ClassVar

import pytest

from boba.tool.kb.confluence.ingest_tools import TOOLS as INGEST_TOOLS
from boba.tool.kb.confluence.ingest_tools import IngestToolConfig


def _config() -> IngestToolConfig:
    return IngestToolConfig.model_validate(
        {
            "connection": {"host": "h", "dbname": "d", "user": "u"},
            "tables": {},
            "embedding": {
                "provider": "local",
                "model": "intfloat/e5",
                "dim": 8,
                "batch_size": 4,
                "progress_every": 1,
            },
            "confluence": {"base_url": "https://confl.example"},
            "tessdata_path": "/usr/share/tessdata",
            "page_workers": 1,
        }
    )


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    pass


class TestIngestOcrParams:
    _NAMES: ClassVar[list[str]] = [
        "confluence_index_pages",
        "confluence_index_cql",
        "confluence_index_spaces",
        "confluence_attachment",
    ]

    def test_module_declares_the_toolset(self) -> None:
        names = [t.name for t in INGEST_TOOLS]
        if names != self._NAMES:
            raise AssertionError("names == self._NAMES")

    def test_llm_params_override_config(self) -> None:
        run_cfg = _config().with_parser(
            ocr_enabled=True, num_workers=3, ocr_language="rus"
        )

        if run_cfg.ocr_enabled is not True:
            raise AssertionError("run_cfg.ocr_enabled is True")
        if run_cfg.num_workers != 3:
            raise AssertionError("run_cfg.num_workers == 3")
        if run_cfg.ocr_language != "rus":
            raise AssertionError('run_cfg.ocr_language == "rus"')

    def test_config_defaults_stay_without_overrides(self) -> None:
        cfg = _config()

        if cfg.ocr_enabled is not False:
            raise AssertionError("cfg.ocr_enabled is False")
        if cfg.num_workers != 1:
            raise AssertionError("cfg.num_workers == 1")
        if cfg.ocr_language != "rus+eng":
            raise AssertionError('cfg.ocr_language == "rus+eng"')

    @pytest.mark.parametrize(
        "name",
        [
            "confluence_index_pages",
            "confluence_index_cql",
            "confluence_index_spaces",
            "confluence_attachment",
        ],
    )
    def test_ocr_controls_are_optional_with_defaults(self, name: str) -> None:
        tools: dict[str, Any] = {tool.name: tool for tool in INGEST_TOOLS}
        schema = tools[name].args_schema.model_json_schema()
        props = schema["properties"]
        if props["ocr_enabled"]["default"] is not False:
            raise AssertionError('props["ocr_enabled"]["default"] is False')
        if props["num_workers"]["default"] != 1:
            raise AssertionError('props["num_workers"]["default"] == 1')
        if props["num_workers"]["maximum"] != 4:
            raise AssertionError('props["num_workers"]["maximum"] == 4')
        if props["ocr_language"]["default"] != "rus+eng":
            raise AssertionError('props["ocr_language"]["default"] == "rus+eng"')
        for control in ("ocr_enabled", "num_workers", "ocr_language"):
            if control in schema.get("required", []):
                raise AssertionError('control not in schema.get("required", [])')
