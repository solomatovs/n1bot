"""Ingest-фасады: настройки OCR из вызова LLM доезжают до конфига прогона."""

from __future__ import annotations

from typing import Any

import pytest

from boba.tool.kb.confluence.ingest_base import ConfluenceIngestConfig
from boba.tool.kb.confluence.ingest_tools import ConfluenceIngestTools

_PROFILE: dict[str, Any] = {
    "rootfs": "",
    "ro_binds": (),
    "rw_binds": (),
    "rw_images": (),
    "image_template": "",
    "launcher": {
        "mount_wait_sec": 10.0,
        "mount_poll_sec": 0.05,
        "shutdown_wait_sec": 5.0,
        "copy_chunk_bytes": 1 << 20,
    },
    "tmpfs": ("/tmp:64M",),  # noqa: S108
    "network": False,
    "env_set": {"PATH": "/usr/bin:/bin"},
    "timeout_sec": 30,
    "max_memory_bytes": 512 * 1024 * 1024,
    "max_cpu_sec": 30,
    "max_file_size_bytes": 64 * 1024 * 1024,
    "max_open_files": 1024,
    "max_processes": 256,
    "max_output_bytes": 256 * 1024,
    "cgroup_base": "",
    "oom_score_adj": 0,
    "cwd": "/tmp",  # noqa: S108
}


def _config() -> ConfluenceIngestConfig:
    return ConfluenceIngestConfig.model_validate(
        {
            "connection": {"host": "db", "user": "kb", "dbname": "kb"},
            "tables": {},
            "embedding": {"model": "intfloat/e5", "dim": 8, "batch_size": 4},
            "confluence": {"base_url": "https://confl.example"},
            "tessdata_path": "/usr/share/tessdata",
            "sandbox": {"profile": _PROFILE, "override": {}},
        }
    )


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    pass


@pytest.fixture
def captured(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Подменяет ingest-функции: фиксирует конфиг, который ушёл в прогон."""
    from boba.tool.kb.confluence import ingest_tools as module
    from boba.toolkit.result import TableResult

    seen: dict[str, Any] = {}

    def fake_pages(cfg: Any, caller: Any, **kw: Any) -> TableResult:
        seen["cfg"] = cfg
        return TableResult(rows=[{}])

    def fake_cql(cfg: Any, caller: Any, **kw: Any) -> dict[str, Any]:
        seen["cfg"] = cfg
        return {}

    def fake_spaces(cfg: Any, caller: Any, **kw: Any) -> TableResult:
        seen["cfg"] = cfg
        return TableResult(rows=[{}])

    def fake_attachment(cfg: Any, caller: Any, **kw: Any) -> str:
        seen["cfg"] = cfg
        return "text"

    monkeypatch.setattr(module, "confluence_ingest_pages", fake_pages)
    monkeypatch.setattr(module, "confluence_ingest_cql", fake_cql)
    monkeypatch.setattr(module, "confluence_ingest_spaces", fake_spaces)
    monkeypatch.setattr(module, "confluence_fetch_attachment", fake_attachment)
    return seen


class _NoLauncher:
    """Исполнитель-заглушка: тесты проверяют обвязку, песочница им не нужна."""

    def call_text(self, command: str, stdin: str) -> Any:
        raise AssertionError("песочница не должна вызываться")

    def call_json(self, entry: Any, request: Any, schema: Any) -> Any:
        raise AssertionError("песочница не должна вызываться")


def _no_launcher(tool: str) -> Any:
    return _NoLauncher()


def _tools() -> dict[str, Any]:
    built = ConfluenceIngestTools(_config(), _no_launcher).build()
    return {tool.name: tool for tool in built}


class TestIngestOcrParams:
    def test_llm_params_override_config(self, captured: dict[str, Any]) -> None:
        tools = _tools()
        tools["confluence_index_pages"].invoke(
            {
                "page_ids": ["1"],
                "ocr_enabled": True,
                "num_workers": 3,
                "ocr_language": "rus",
            }
        )
        cfg = captured["cfg"]
        assert cfg.ocr_enabled is True
        assert cfg.num_workers == 3
        assert cfg.ocr_language == "rus"

    def test_facade_defaults_apply(self, captured: dict[str, Any]) -> None:
        tools = _tools()
        tools["confluence_index_cql"].invoke({"cql": "space = DQ"})
        cfg = captured["cfg"]
        assert cfg.ocr_enabled is False
        assert cfg.num_workers == 1
        assert cfg.ocr_language == "rus+eng"

    def test_attachment_carries_overrides(self, captured: dict[str, Any]) -> None:
        tools = _tools()
        tools["confluence_attachment"].invoke(
            {
                "page_id": "1",
                "filename": "report.pdf",
                "ocr_enabled": True,
                "num_workers": 2,
                "ocr_language": "eng",
            }
        )
        cfg = captured["cfg"]
        assert cfg.ocr_enabled is True
        assert cfg.num_workers == 2
        assert cfg.ocr_language == "eng"

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
        tools = _tools()
        schema = tools[name].get_input_schema().model_json_schema()
        props = schema["properties"]
        assert props["ocr_enabled"]["default"] is False
        assert props["num_workers"]["default"] == 1
        assert props["num_workers"]["maximum"] == 4
        assert props["ocr_language"]["default"] == "rus+eng"
        for control in ("ocr_enabled", "num_workers", "ocr_language"):
            assert control not in schema.get("required", [])
