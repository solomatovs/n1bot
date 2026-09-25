"""Сборщик запросов ch: строка встаёт в текст голой на место $name, любое другое
значение уезжает параметром драйвера, а подставляет его драйвер — серверу
{name:Type} или сам на месте %(name)s, квотируя ChIdentifier своим
quote_identifier. Куски без строк билдер не трогает."""

from __future__ import annotations

import pytest
from clickhouse_connect.driver.binding import bind_query

from boba.db.clickhouse.query import (
    ChIdentifier,
    ChIdentifiers,
    ChQueryBuilder,
    ChValue,
)
from boba.toolkit.sql import QueryBuildError


class TestBareText:
    def test_string_goes_into_the_text_as_is(self) -> None:
        query = (
            ChQueryBuilder(fmt="TabSeparated")
            .add("select $columns from $table", columns="a, b", table="db.t")
            .add("format $fmt")
            .build()
        )

        assert query.text == "select a, b from db.t\nformat TabSeparated"
        assert query.params is None

    def test_piece_without_strings_is_left_untouched(self) -> None:
        query = ChQueryBuilder().add("select '$x' where a = {a:UInt8}", a=1).build()

        assert query.text == "select '$x' where a = {a:UInt8}"
        assert query.params == {"a": 1}

    def test_literal_dollar_is_doubled_in_a_piece_with_strings(self) -> None:
        query = ChQueryBuilder().add("select '$$', $col", col="c").build()

        assert query.text == "select '$', c"

    def test_missing_name_is_refused(self) -> None:
        with pytest.raises(QueryBuildError, match=r"expects only names col"):
            ChQueryBuilder().add("select $other", col="c")


class TestServerMode:
    def test_values_stay_parameters(self) -> None:
        query = (
            ChQueryBuilder()
            .add("select * from {table:Identifier}", table=ChValue("events"))
            .add("where name = {name:String}", name=ChValue("o'neil"))
            .build()
        )

        assert (
            query.text == "select * from {table:Identifier}\nwhere name = {name:String}"
        )
        assert query.params == {"table": "events", "name": "o'neil"}

        text, params = bind_query(query.text, query.params, None)

        assert text == query.text
        assert params == {"param_table": "events", "param_name": "o\\'neil"}

    def test_false_condition_leaves_the_piece_out(self) -> None:
        query = (
            ChQueryBuilder()
            .add("select 1 where true")
            .when(False, "and a = {a:UInt8}", a=1)
            .when(True, "and b = {b:UInt8}", b=2)
            .build()
        )

        assert query.text == "select 1 where true\nand b = {b:UInt8}"
        assert query.params == {"b": 2}

    def test_same_parameter_with_the_same_value_is_fine(self) -> None:
        query = (
            ChQueryBuilder()
            .add("where a = {a:String}", a=ChValue("x"))
            .add("or b = {a:String}", a=ChValue("x"))
            .build()
        )

        assert query.params == {"a": "x"}

    def test_same_parameter_with_another_value_is_refused(self) -> None:
        builder = ChQueryBuilder().add("where a = {a:UInt8}", a=1)

        with pytest.raises(QueryBuildError, match="bound twice"):
            builder.add("or b = {a:UInt8}", a=2)


class TestClientMode:
    def test_identifiers_and_literals_are_rendered_by_the_driver(self) -> None:
        query = (
            ChQueryBuilder()
            .add(
                "select %(columns)s from %(db)s.%(table)s",
                columns=ChIdentifiers(["id", "na me"]),
                db=ChIdentifier("we`ird"),
                table=ChIdentifier("t"),
            )
            .add("where name = %(name)s and share > 5 %% 2", name=ChValue("o'neil"))
            .add("format $fmt", fmt="CSV")
            .build()
        )

        text, params = bind_query(query.text, query.params, None)

        assert text == (
            "select `id`, `na me` from `we\\`ird`.`t`\n"
            "where name = 'o\\'neil' and share > 5 % 2\n"
            "format CSV"
        )
        assert params == {}
