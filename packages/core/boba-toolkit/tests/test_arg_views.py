"""Модель вызова: форма studio по полям, показ входа объявленными результатами."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Any, Literal

import pytest
from pydantic import BaseModel, Field, SecretStr, ValidationError

from boba.toolkit.calls import (
    CallViews,
    FieldPlacement,
    FieldView,
    ToolCallBase,
    ToolCallModels,
)
from boba.toolkit.facade import Injected, tool
from boba.toolkit.result import (
    ChatView,
    FieldLines,
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
    connection_name: str
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


def test_structured_arguments_keep_their_shape() -> None:
    """Модель — компактный json в строку, список объектов — таблица,
    длинная структура — блок json с отступами."""
    call = Args(
        sql="select 1",
        connection_name="main",
        limits=Limits(rows=10),
        tags=["x" * 70, "y" * 70],
    )
    markdown = call.chat_view().markdown
    assert '**limits:** `{"rows": 10}`' in markdown
    assert '**tags:**\n```json\n[\n  "' in markdown

    rows = FieldLines.line(
        "columns",
        [{"name": "id", "nullable": False}, {"name": "note", "type_text": "text"}],
    )
    assert rows.startswith("**columns:**\n\n| name")
    assert "| note | " in rows


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
        def bash1(
            call: BashCall, *, cfg: Annotated[Limits, Injected]
        ) -> MarkdownResult:
            """Bash."""
            return MarkdownResult(text=f"{call.command}|{cfg.rows}")

        kwargs = bash1.packed_kwargs({"command": "ls", "cfg": Limits(rows=3)})
        assert isinstance(kwargs["call"], BashCall)
        assert bash1.func is not None
        assert bash1.func(**kwargs).text == "ls|3"
        assert list(bash1.args_schema.model_fields) == ["command", "stdin", "cfg"]

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


class TestViewsTravelWithTheSchema:
    """Вид аргументов едет со схемой инструмента: клиент без кода инструмента
    рисует вход шага так же, как процесс, где инструмент объявлен."""

    ARGS: dict[str, Any] = {
        "sql": "select 1",
        "connection_name": "main",
        "top_k": 7,
        "intent": "count the rows",
        "stdin": "",
    }

    def test_remote_call_is_shown_like_the_local_one(self) -> None:
        views = CallViews()
        schema = views.marked(Args.model_json_schema(), views.of(Args))

        read = views.read(schema)
        if read is None:
            raise AssertionError(f"the schema carries the views: {schema}")
        if read != views.of(Args):
            raise AssertionError(f"the views survive the wire: {read}")

        model = views.model_of("remote_probe", read)
        local = Args.model_construct(**self.ARGS).chat_view().markdown
        remote = model.model_construct(**self.ARGS).chat_view().markdown
        if remote != local:
            raise AssertionError(f"same step input:\n{remote}\n---\n{local}")
        if "```sql" not in remote:
            raise AssertionError(f"the declared display draws the value: {remote}")

    def test_views_name_only_what_differs_from_a_plain_line(self) -> None:
        views = CallViews().of(Args)

        expected = {
            "sql": FieldView(
                placement=FieldPlacement.BODY,
                display=MarkdownResult(language="sql").model_dump(mode="json"),
            ),
            "intent": FieldView(placement=FieldPlacement.HEADER, display=None),
            "cfg": FieldView(placement=FieldPlacement.HIDDEN, display=None),
        }
        if dict(views) != expected:
            raise AssertionError(f"the views of the call model: {views}")

    def test_schema_without_the_mark_gives_no_views(self) -> None:
        if CallViews().read({"type": "object"}) is not None:
            raise AssertionError("a foreign tool has no call views")

    def test_tool_without_views_is_left_unmarked(self) -> None:
        schema = CallViews().marked({"type": "object"}, None)
        if CallViews.MARK in schema:
            raise AssertionError(f"nothing to say about such a tool: {schema}")

    def test_broken_mark_is_refused(self) -> None:
        schema = {"type": "object", CallViews.MARK: {"sql": {"placement": "above"}}}

        with pytest.raises(ValidationError, match="placement"):
            CallViews().read(schema)
