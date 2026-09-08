"""Модель вызова: форма studio по полям, показ входа объявленными результатами."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

import pytest
from pydantic import BaseModel, Field, SecretStr

from boba.toolkit.calls import (
    BoolEditor,
    ConnectionEditor,
    FieldPlacement,
    JsonEditor,
    NumberEditor,
    SecretEditor,
    SelectEditor,
    StudioField,
    TextEditor,
    ToolCallBase,
    ToolCallModels,
)
from boba.toolkit.facade import Injected, tool
from boba.toolkit.result import (
    ChatView,
    MarkdownResult,
    ResultKindError,
    ResultKinds,
    ShellResult,
    TableResult,
    ToolResultBase,
)


class Mode(StrEnum):
    FAST = "fast"
    SAFE = "safe"


class Limits(BaseModel):
    rows: int


class Args(ToolCallBase):
    sql: Annotated[
        str, Field(min_length=1, description="query"), MarkdownResult(language="sql")
    ]
    connection_name: Annotated[str, ConnectionEditor(family="postgres")]
    stdin: Annotated[str, Field(max_length=4000)] = ""
    top_k: Annotated[int, Field(ge=1, le=50)] = 5
    ratio: float | None = None
    strict: bool = False
    mode: Mode = Mode.FAST
    kind: Literal["a", "b"] = "a"
    token: SecretStr | None = None
    limits: Limits | None = None
    tags: list[str] = []
    intent: str = ""
    cfg: Annotated[Limits, Injected] = Limits(rows=1)


def _field(name: str) -> StudioField:
    for field in Args.studio_view().fields:
        if field.name == name:
            return field

    raise AssertionError(f"no field {name}")


def test_declared_editor_wins() -> None:
    assert _field("connection_name").editor == ConnectionEditor(family="postgres")


def test_display_is_the_declared_result() -> None:
    field = _field("sql")
    assert field.display == MarkdownResult(language="sql")
    assert field.editor == TextEditor()
    assert field.description == "query"
    assert field.required


def test_intent_goes_to_header_and_injected_is_hidden() -> None:
    assert _field("intent").placement is FieldPlacement.HEADER
    assert _field("cfg").placement is FieldPlacement.HIDDEN


def test_inferred_editors() -> None:
    assert _field("stdin").editor == TextEditor(multiline=True)
    assert _field("top_k").editor == NumberEditor(minimum=1, maximum=50)
    assert _field("ratio").editor == NumberEditor()
    assert _field("strict").editor == BoolEditor()
    assert _field("mode").editor == SelectEditor(options=("fast", "safe"))
    assert _field("kind").editor == SelectEditor(options=("a", "b"))
    assert _field("token").editor == SecretEditor()
    assert _field("limits").editor == JsonEditor()
    assert _field("tags").editor == JsonEditor()


def test_llm_view_hides_injected() -> None:
    call = Args(sql="select 1", connection_name="main", cfg=Limits(rows=2))
    assert '"cfg"' not in call.llm_view()
    assert '"sql": "select 1"' in call.llm_view()


def test_chat_view_uses_declared_results_and_field_lines() -> None:
    call = Args(sql="select 1", connection_name="main", token=SecretStr("x"))
    markdown = call.chat_view().markdown
    assert markdown.startswith("```sql\nselect 1\n```")
    assert "**connection_name:** `main`" in markdown
    assert "x" not in markdown.split("`main`")[1]
    assert "**stdin" not in markdown
    assert "intent" not in markdown


def test_call_of_renders_unknown_tool_as_json() -> None:
    markdown = ToolCallModels.call_of("no_such_tool", {"a": 1}).chat_view().markdown
    assert markdown.startswith("```json")


def test_result_kinds_of_return_annotation() -> None:
    assert ResultKinds.kinds_of(TableResult | ShellResult) == ("table", "shell")
    assert ResultKinds.kinds_of(ShellResult) == ("shell",)
    assert ResultKinds.kinds_of(ToolResultBase) == ()


def test_result_kinds_rejects_foreign_annotation() -> None:
    with pytest.raises(ResultKindError):
        ResultKinds.kinds_of(str)

    with pytest.raises(ResultKindError):
        ResultKinds.kinds_of(tuple[str, ShellResult])


def test_display_in_annotated_keeps_field_type() -> None:
    parsed = Args(sql="select 1", connection_name="main")
    assert parsed.sql == "select 1"

    with pytest.raises(ValueError, match="sql"):
        Args(sql="", connection_name="main")


class BashCall(ToolCallBase):
    command: Annotated[str, Field(min_length=1), MarkdownResult(language="bash")]
    stdin: str = ""

    def chat_view(self) -> ChatView:
        return ChatView(markdown=f"custom:{self.command}")


class TestDeclaredCallClass:
    def test_body_receives_the_model(self) -> None:
        @tool
        def bash(call: BashCall, *, cfg: Annotated[Limits, Injected]) -> MarkdownResult:
            """Bash."""
            return MarkdownResult(text=f"{call.command}|{cfg.rows}")

        kwargs = bash.packed_kwargs({"command": "ls", "cfg": Limits(rows=3)})
        assert isinstance(kwargs["call"], BashCall)
        assert bash.func is not None
        assert bash.func(**kwargs).text == "ls|3"
        assert list(bash.args_schema.model_fields) == ["command", "stdin", "cfg"]

    def test_custom_chat_view_survives_registration(self) -> None:
        @tool
        def bash2(call: BashCall) -> MarkdownResult:
            """Bash."""
            return MarkdownResult(text=call.command)

        assert (
            ToolCallModels.call_of("bash2", {"command": "ls"}).chat_view().markdown
            == "custom:ls"
        )

    def test_llm_argument_next_to_the_model_is_refused(self) -> None:
        with pytest.raises(Exception, match="must be injected or a port"):

            @tool
            def bad(call: BashCall, extra: str) -> MarkdownResult:
                """Bad."""
                return MarkdownResult(text=extra)
