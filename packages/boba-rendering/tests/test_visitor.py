"""Visitor pattern: double-dispatch без isinstance, типизированный возврат."""

from __future__ import annotations

import json

from boba.rendering import (
    JsonResult,
    TextResult,
    ToolResult,
    ToolResultVisitor,
)


class _StrVisitor(ToolResultVisitor[str]):
    def visit_text(self, result: TextResult) -> str:
        return result.text

    def visit_json(self, result: JsonResult) -> str:
        return json.dumps(result.payload, ensure_ascii=False)


class _DictVisitor(ToolResultVisitor[dict]):
    def visit_text(self, result: TextResult) -> dict:
        return {"type": "text", "text": result.text}

    def visit_json(self, result: JsonResult) -> dict:
        return {"type": "json", "data": result.payload}


def test_text_dispatches_to_visit_text():
    res: ToolResult = TextResult(text="hello")
    assert res.accept(_StrVisitor()) == "hello"


def test_json_dispatches_to_visit_json():
    res: ToolResult = JsonResult(payload={"k": "v"})
    rendered = res.accept(_StrVisitor())
    assert json.loads(rendered) == {"k": "v"}


def test_visitor_return_type_is_polymorphic():
    txt: ToolResult = TextResult(text="x")
    jsn: ToolResult = JsonResult(payload=[1, 2])
    dv = _DictVisitor()
    assert txt.accept(dv) == {"type": "text", "text": "x"}
    assert jsn.accept(dv) == {"type": "json", "data": [1, 2]}


def test_unicode_preserved_in_json_visitor():
    rendered = JsonResult(payload={"k": "Привет"}).accept(_StrVisitor())
    assert "Привет" in rendered
