"""Вид аргумента: объявление у поля через Annotated, иначе вывод по типу."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

import pytest
from pydantic import BaseModel, Field, SecretStr

from boba.toolkit.calls import (
    ArgPlacement,
    ArgView,
    ArgViews,
    BoolArg,
    CodeArg,
    ConnectionArg,
    EnumArg,
    IntentArg,
    JsonArg,
    NumberArg,
    ScriptCall,
    SecretArg,
    TextArg,
    ToolCallView,
    ToolCallViews,
)
from boba.toolkit.result import Produces, ShellResult, TableResult


class Mode(StrEnum):
    FAST = "fast"
    SAFE = "safe"


class Limits(BaseModel):
    rows: int


class Args(BaseModel):
    sql: Annotated[str, Field(min_length=1, description="query")]
    connection_name: Annotated[str, ConnectionArg(family="postgres")]
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


def _view(name: str, call: ToolCallView = ToolCallViews.DEFAULT) -> ArgView:
    return ArgViews.of_field(name, Args.model_fields[name], call)


def test_declared_view_wins() -> None:
    assert _view("connection_name") == ConnectionArg(family="postgres")


def test_script_call_makes_its_arg_code() -> None:
    assert _view("sql", ScriptCall(arg="sql", lang="sql")) == CodeArg(lang="sql")
    assert _view("sql") == TextArg()


def test_intent_goes_to_header() -> None:
    view = _view("intent")
    assert view == IntentArg()
    assert view.placement is ArgPlacement.HEADER


def test_inferred_kinds() -> None:
    assert _view("stdin") == TextArg(multiline=True)
    assert _view("top_k") == NumberArg(minimum=1, maximum=50)
    assert _view("ratio") == NumberArg()
    assert _view("strict") == BoolArg()
    assert _view("mode") == EnumArg(options=("fast", "safe"))
    assert _view("kind") == EnumArg(options=("a", "b"))
    assert _view("token") == SecretArg()
    assert _view("limits") == JsonArg()
    assert _view("tags") == JsonArg()


def test_produces_lists_result_kinds() -> None:
    assert Produces.of(TableResult, ShellResult).kinds == ("table", "shell")


class Rebuilt(BaseModel):
    """Схема, пересобранная приложением: вид в метаданных не подменяет тип поля."""

    connection_name: Annotated[
        str, Field(min_length=1), ConnectionArg(family="postgres")
    ]
    limits: Annotated[Limits, JsonArg()]


def test_view_in_annotated_keeps_field_type() -> None:
    parsed = Rebuilt(connection_name="main", limits=Limits(rows=1))
    assert parsed.connection_name == "main"
    assert (
        ConnectionArg(family="postgres")
        in Rebuilt.model_fields["connection_name"].metadata
    )

    with pytest.raises(ValueError, match="connection_name"):
        Rebuilt(connection_name="", limits=Limits(rows=1))


def test_view_as_field_still_validates_and_dumps() -> None:
    class Holder(BaseModel):
        view: ConnectionArg

    dumped = Holder(view=ConnectionArg(family="postgres")).model_dump()
    assert dumped == {
        "view": {"placement": "body", "kind": "connection", "family": "postgres"}
    }
    assert (
        Holder.model_validate(
            {"view": {"kind": "connection", "family": "x"}}
        ).view.family
        == "x"
    )
