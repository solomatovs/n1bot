"""Ingest-функции: фасад LLM сведён к цели, attachments и ocr."""

from __future__ import annotations

from typing import Any, ClassVar

import pytest

from boba.tool.kb.confluence.ingest_tools import TOOLS as INGEST_TOOLS
from boba.tool.kb.confluence.ingest_tools import IngestToolConfig


def _config() -> IngestToolConfig:
    return IngestToolConfig.model_validate(
        {
            "connection": {
                "host": "h",
                "dbname": "d",
                "auth": {"method": "trust", "user": "u"},
            },
            "tables": {"sources_table": "kb_sources"},
            "embedding": {
                "kind": "local",
                "model": "intfloat/e5",
                "dim": 8,
                "batch_size": 4,
                "progress_every": 1,
            },
            "confluence": {"host": "confl.example", "port": 443},
            "attachments": ["application/pdf", "*.txt"],
            "text_encodings": ["utf-8"],
            "tessdata_path": "/usr/share/tessdata",
            "ocr_language": "rus",
            "num_workers": 3,
            "page_workers": 1,
        }
    )


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    pass


class TestIngestOcrParams:
    _NAMES: ClassVar[list[str]] = [
        "confluence_index_page",
        "confluence_index_cql",
        "confluence_index_space",
        "confluence_attachment",
    ]

    _INDEX_NAMES: ClassVar[list[str]] = [
        "confluence_index_page",
        "confluence_index_cql",
        "confluence_index_space",
    ]

    def test_module_declares_the_toolset(self) -> None:
        names = [t.name for t in INGEST_TOOLS]
        if names != self._NAMES:
            raise AssertionError("names == self._NAMES")

    def test_call_ocr_overrides_config_and_keeps_admin_settings(self) -> None:
        run_cfg = _config().with_ocr(ocr=True)

        if run_cfg.ocr_enabled is not True:
            raise AssertionError("run_cfg.ocr_enabled is True")
        if run_cfg.num_workers != 3:
            raise AssertionError("ocr workers come from the config")
        if run_cfg.ocr_language != "rus":
            raise AssertionError("ocr language comes from the config")

    def test_config_ocr_is_off_until_the_call_asks(self) -> None:
        cfg = _config()

        if cfg.ocr_enabled is not False:
            raise AssertionError("cfg.ocr_enabled is False")

    @pytest.mark.parametrize("name", _INDEX_NAMES)
    def test_index_tools_take_only_target_attachments_and_ocr(self, name: str) -> None:
        tools: dict[str, Any] = {tool.name: tool for tool in INGEST_TOOLS}
        schema = tools[name].args_schema.model_json_schema()
        props = schema["properties"]

        optional = {"attachments", "ocr", "cfg"}
        if set(props) - optional != {"page_id", "cql", "space_key"} & set(props):
            raise AssertionError(f"unexpected parameters: {sorted(props)}")
        if props["attachments"]["default"] is not False:
            raise AssertionError("attachments default to false")
        if props["ocr"]["default"] is not False:
            raise AssertionError("ocr defaults to false")
        for control in ("attachments", "ocr"):
            if control in schema.get("required", []):
                raise AssertionError(f"{control} must be optional")

    def test_attachment_tool_takes_only_ocr(self) -> None:
        tools: dict[str, Any] = {tool.name: tool for tool in INGEST_TOOLS}
        schema = tools["confluence_attachment"].args_schema.model_json_schema()
        props = schema["properties"]

        if set(props) != {"page_id", "filename", "ocr", "cfg"}:
            raise AssertionError(f"unexpected parameters: {sorted(props)}")
        if props["ocr"]["default"] is not False:
            raise AssertionError("ocr defaults to false")
