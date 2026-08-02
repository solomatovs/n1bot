"""Порт boba.tool.doc: чтение документов из workspace-образа пользователя."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from boba.chainlit2.agent.tools.doc import DocToolsConfig, build_doc_tools
from boba.chainlit2.agent.tools.doc import engine as engine_module
from boba.chainlit2.agent.tools.doc.engine import DocEngine
from boba.chainlit2.agent.tools.doc.tools import DocSearch
from boba.chainlit2.infra.config import LocalStorageConfig


def _storage_cfg(**kw: Any) -> LocalStorageConfig:
    """Тайминги лаунчера обязательны: дефолтов у конфига нет."""
    fields: dict[str, Any] = {
        "launcher": {
            "mount_wait_sec": 10.0,
            "mount_poll_sec": 0.05,
            "shutdown_wait_sec": 5.0,
            "copy_chunk_bytes": 1 << 20,
        },
    }
    fields.update(kw)
    return LocalStorageConfig.model_validate(fields)


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    pass


@pytest.fixture(autouse=True)
def session_user(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(engine_module, "current_user_id", lambda: "7")


def _config(**kw: Any) -> DocToolsConfig:
    storage = _storage_cfg(
        kind="image",
        image_path="/ws/{user_id}.ext4",
        image_template="/t.ext4",
    )
    return DocToolsConfig(storage=storage, **kw)


class TestObjectKey:
    """Путь песочницы отображается в ключ хранилища вложений."""

    def test_sandbox_path_maps_to_storage_key(self) -> None:
        key = DocEngine.object_key("/workspace/t1/upload/report.pdf")
        assert key == "7/t1/upload/report.pdf"

    def test_relative_path_is_accepted(self) -> None:
        assert DocEngine.object_key("t1/upload/a.pdf") == "7/t1/upload/a.pdf"

    def test_name_with_spaces_and_cyrillic(self) -> None:
        key = DocEngine.object_key("/workspace/t1/upload/отчёт за май.pdf")
        assert key == "7/t1/upload/отчёт за май.pdf"

    @pytest.mark.parametrize(
        "path", ["/workspace/../../etc/passwd", "../secret", "/workspace", ""]
    )
    def test_escape_rejected(self, path: str) -> None:
        with pytest.raises(RuntimeError, match="invalid document path"):
            DocEngine.object_key(path)

    def test_without_session_fails_loudly(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(engine_module, "current_user_id", lambda: None)
        with pytest.raises(RuntimeError, match="no chainlit session"):
            DocEngine.object_key("/workspace/t1/upload/a.pdf")


class TestBuild:
    def test_all_tools_registered(self) -> None:
        names = [t.name for t in build_doc_tools(_config())]
        assert names == [
            "read_document",
            "read_pages",
            "read_document_window",
            "document_outline",
            "search_document",
        ]

    def test_missing_file_reports_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        engine = DocEngine(_config())

        async def missing(key: str) -> bytes:
            raise FileNotFoundError(key)

        monkeypatch.setattr(engine._storage, "read_file", missing)
        with pytest.raises(RuntimeError, match="file not found"):
            asyncio.run(engine.read_bytes("/workspace/t1/upload/nope.pdf"))

    def test_window_wider_than_limit_rejected(self) -> None:
        tools = {t.name: t for t in build_doc_tools(_config(max_text_chars=100))}
        window = tools["read_document_window"]
        with pytest.raises(RuntimeError, match="exceeds max_text_chars"):
            asyncio.run(
                window.ainvoke(
                    {"path": "/workspace/t1/upload/a.pdf", "start_char": 0,
                     "length": 500}
                )
            )


class TestTextHelpers:
    def test_clip_marks_truncation(self) -> None:
        text, truncated = DocEngine.clip("a" * 50, 10)
        assert truncated is True
        assert len(text) == 10

    def test_clip_keeps_short_text(self) -> None:
        text, truncated = DocEngine.clip("short", 10)
        assert (text, truncated) == ("short", False)

    def test_window_reports_more(self) -> None:
        chunk, end, total, has_more = DocEngine.window("abcdef", 2, 2)
        assert (chunk, end, total, has_more) == ("cd", 4, 6, True)

    def test_window_at_the_end(self) -> None:
        chunk, end, total, has_more = DocEngine.window("abcdef", 4, 10)
        assert (chunk, end, total, has_more) == ("ef", 6, 6, False)


class _Hit:
    def __init__(self, text: str) -> None:
        self.text = text
        self.x = 1.0
        self.y = 2.0
        self.width = 3.0
        self.height = 4.0


class _Page:
    def __init__(self, text: str) -> None:
        self.page_num = 1
        self.text = text
        self.text_items: list[str] = []


class _Native:
    def __init__(self, text: str) -> None:
        self.pages = [_Page(text)]


class TestSearchRows:
    """Сниппет собирается из текста страницы: нативный поиск его не даёт."""

    @staticmethod
    def _rows(text: str, query: str, monkeypatch: pytest.MonkeyPatch, **kw: Any):
        from boba.chainlit2.agent.tools.doc import tools as tools_module

        monkeypatch.setattr(
            tools_module.LiteParseEngine,
            "search_items",
            staticmethod(lambda items, q, case_sensitive=False: [_Hit(q)]),
        )
        return DocSearch.run(_Native(text), query, kw.get("context", 5), 50)

    def test_snippet_has_context_and_ellipsis(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rows = self._rows("х" * 20 + "цель" + "у" * 20, "цель", monkeypatch)
        assert rows[0]["snippet"].startswith("…")
        assert "цель" in rows[0]["snippet"]
        assert rows[0]["snippet"].endswith("…")

    def test_row_carries_coordinates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        rows = self._rows("цель", "цель", monkeypatch)
        assert rows[0]["page"] == 1
        assert rows[0]["x"] == 1.0
        assert rows[0]["height"] == 4.0
