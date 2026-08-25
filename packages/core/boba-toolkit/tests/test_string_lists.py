"""StringList/LLMStringList: CSV по правилам csv, json-список, одно значение."""

from __future__ import annotations

from pydantic import BaseModel

from boba.toolkit.types import LLMStringList, StringList


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


class TestLLMStringList:
    def test_json_list(self) -> None:
        assert spaces_of('["A", "B"]') == ["A", "B"]

    def test_csv_and_single(self) -> None:
        assert spaces_of("A, B") == ["A", "B"]
        assert spaces_of("A") == ["A"]

    def test_quoted_comma(self) -> None:
        assert spaces_of('"a,b", c') == ["a,b", "c"]
