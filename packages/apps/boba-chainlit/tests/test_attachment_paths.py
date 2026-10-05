"""Пути вложений вопроса: модель их видит, лента — нет."""

from __future__ import annotations

import pytest
from langchain_core.messages import HumanMessage

from boba.canvas.keys import ObjectKey
from boba.chainlit.infra.providers import build_llm_view

MOUNT = "/workspace"
"""Точка workspace в песочнице: под ней лежат каталоги тредов."""


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    """Пути вложений не зависят от сессии chainlit."""


class TestAttachmentPaths:
    """Пути вложений уходят в LLM, но не в ленту."""

    @staticmethod
    def _message(*attachments: dict[str, str]) -> HumanMessage:
        extra = {"attachments": list(attachments)} if attachments else {}
        return HumanMessage(content="разбери файл", id="u1", additional_kwargs=extra)

    def test_llm_sees_sandbox_path(self) -> None:
        msg = self._message({"name": "data.csv", "path": "/workspace/upload/el-1"})
        if "/workspace/upload/el-1" not in build_llm_view([msg])[0].content:
            raise AssertionError('"/workspace/upload/el-1" in build_llm_view([msg])[0…')

    def test_llm_sees_file_name(self) -> None:
        msg = self._message({"name": "data.csv", "path": "/workspace/upload/el-1"})
        if "data.csv" not in build_llm_view([msg])[0].content:
            raise AssertionError('"data.csv" in build_llm_view([msg])[0].content')

    def test_original_message_is_not_touched(self) -> None:
        msg = self._message({"name": "data.csv", "path": "/workspace/upload/el-1"})
        build_llm_view([msg])
        if msg.content != "разбери файл":
            raise AssertionError('msg.content == "разбери файл"')

    def test_message_without_attachments_unchanged(self) -> None:
        if build_llm_view([self._message()])[0].content != "разбери файл":
            raise AssertionError('build_llm_view([self._message()])[0].content == "ра…')

    def test_several_attachments_listed(self) -> None:
        msg = self._message(
            {"name": "a.csv", "path": "/workspace/upload/1"},
            {"name": "b.csv", "path": "/workspace/upload/2"},
        )
        content = build_llm_view([msg])[0].content
        if "a.csv" not in content:
            raise AssertionError('"a.csv" in content')
        if "b.csv" not in content:
            raise AssertionError('"b.csv" in content')

    def test_path_matches_where_storage_puts_the_file(self) -> None:
        key = ObjectKey.build("4", "t-1", "report.pdf", "el-1")
        if key.render() != "4/t-1/upload/report.pdf":
            raise AssertionError('key.render() == "4/t-1/upload/report.pdf"')
        if f"{MOUNT}/{key.in_thread()}" != "/workspace/t-1/upload/report.pdf":
            raise AssertionError('f"{MOUNT}/{key.in_thread()}" == ( "/works…')

    def test_unnamed_element_falls_back_to_id(self) -> None:
        key = ObjectKey.build("4", "t-1", "", "el-1")
        if key.render() != "4/t-1/upload/el-1":
            raise AssertionError('key.render() == "4/t-1/upload/el-1"')

    def test_directories_in_name_are_stripped(self) -> None:
        key = ObjectKey.build("4", "t-1", "../../etc/passwd", "el-1")
        if key.render() != "4/t-1/upload/passwd":
            raise AssertionError('key.render() == "4/t-1/upload/passwd"')

    def test_parse_is_inverse_of_render(self) -> None:
        key = ObjectKey.build("4", "t-1", "report.pdf", "el-1")
        if ObjectKey.parse(key.render()) != key:
            raise AssertionError("ObjectKey.parse(key.render()) == key")

    def test_parse_rejects_traversal_and_alien_layout(self) -> None:
        for raw in ("4/t-1/upload/..", "4/t-1/other/report.pdf", "4/t-1/report.pdf"):
            with pytest.raises(ValueError, match="invalid object_key"):
                ObjectKey.parse(raw)
