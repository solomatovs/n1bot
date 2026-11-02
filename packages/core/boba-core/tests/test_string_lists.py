"""StringList/LLMStringList: CSV по правилам csv, json-список, одно значение."""

from __future__ import annotations

import pytest
from pydantic import BaseModel, ValidationError

from boba.core.types import LLMStringList, StringList


class Config(BaseModel):
    tools: StringList = []


class Args(BaseModel):
    spaces: LLMStringList = []


def tools_of(raw: object) -> list[str]:
    return Config.model_validate({"tools": raw}).tools


def spaces_of(raw: object) -> list[str]:
    return Args.model_validate({"spaces": raw}).spaces


class TestStringList:
    def test_plain_csv(self) -> None:
        assert tools_of("a, b ,c,,") == ["a", "b", "c"]

    def test_quoted_item_keeps_its_comma(self) -> None:
        assert tools_of('"report-{a,b}.pdf", plain') == ["report-{a,b}.pdf", "plain"]

    def test_list_passes_through(self) -> None:
        assert tools_of(["x", "y"]) == ["x", "y"]

    def test_blank_lines_and_empty_items_are_omitted(self) -> None:
        assert tools_of('  ,\n a,, "b,c"\n\n d, ') == ["a", "b,c", "d"]

    @pytest.mark.parametrize("raw", [None, 42, {"name": "a"}, [1]])
    def test_invalid_input_is_rejected_at_the_model(self, raw: object) -> None:
        with pytest.raises(ValidationError):
            tools_of(raw)


class TestLLMStringList:
    def test_json_list(self) -> None:
        assert spaces_of('["A", "B"]') == ["A", "B"]

    def test_csv_and_single(self) -> None:
        assert spaces_of("A, B") == ["A", "B"]
        assert spaces_of("A") == ["A"]

    def test_quoted_comma(self) -> None:
        assert spaces_of('"a,b", c') == ["a,b", "c"]

    def test_json_scalars_are_rendered_as_strings(self) -> None:
        assert spaces_of('[1, true, null, "a,b"]') == ["1", "True", "None", "a,b"]

    def test_invalid_json_is_rejected_at_the_model(self) -> None:
        with pytest.raises(ValidationError):
            spaces_of('["unfinished"')
