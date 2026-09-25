"""Сборщик запросов ch: куски склеиваются подряд без разбора текста, значения
уезжают параметрами драйвера, а подставляет их драйвер — серверу {name:Type}
или сам на месте %(name)s, квотируя ChIdentifier своим quote_identifier."""

from __future__ import annotations

import pytest
from clickhouse_connect.driver.binding import bind_query

from boba.db.clickhouse.query import ChIdentifier, ChIdentifiers, ChQueryBuilder
from boba.toolkit.sql import QueryBuildError


class TestPieces:
    def test_pieces_join_and_adds_break_lines(self) -> None:
        query = (
            ChQueryBuilder()
            .add("select ", "a, b", " from ", "db.t")
            .add("format ", "TabSeparated")
            .build()
        )

        assert query.text == "select a, b from db.t\nformat TabSeparated"
        assert query.params is None

    def test_text_is_not_parsed(self) -> None:
        query = ChQueryBuilder().add("select '$x', '{y}', '%(z)s' from t").build()

        assert query.text == "select '$x', '{y}', '%(z)s' from t"
        assert query.params is None


class TestServerMode:
    def test_values_stay_parameters(self) -> None:
        query = (
            ChQueryBuilder()
            .add("select * from {table:Identifier}", table="events")
            .add("where name = {name:String}", name="o'neil")
            .build()
        )

        assert (
            query.text == "select * from {table:Identifier}\nwhere name = {name:String}"
        )
        assert query.params == {"table": "events", "name": "o'neil"}

    def test_false_condition_leaves_the_piece_out(self) -> None:
        query = (
            ChQueryBuilder()
            .add("select 1")
            .when(False, "where a = {a:UInt8}", a=1)
            .when(True, "where b = {b:UInt8}", b=2)
            .build()
        )

        assert query.text == "select 1\nwhere b = {b:UInt8}"
        assert query.params == {"b": 2}

    def test_same_parameter_with_the_same_value_is_fine(self) -> None:
        query = (
            ChQueryBuilder()
            .add("select {a:UInt8}", a=1)
            .add("union all select {a:UInt8}", a=1)
            .build()
        )

        assert query.params == {"a": 1}

    def test_same_parameter_with_another_value_is_refused(self) -> None:
        builder = ChQueryBuilder().add("select {a:UInt8}", a=1)

        with pytest.raises(QueryBuildError, match="bound twice"):
            builder.add("union all select {a:UInt8}", a=2)

    def test_built_query_in_bind_is_refused(self) -> None:
        inner = ChQueryBuilder().add("select 1").build()

        with pytest.raises(QueryBuildError, match="built query"):
            ChQueryBuilder().add("select * from ({q:String})", q=inner)


class TestClientMode:
    def test_identifiers_and_literals_are_rendered_by_the_driver(self) -> None:
        query = (
            ChQueryBuilder()
            .add(
                "select %(columns)s from %(db)s.%(t)s where s = %(s)s and n = %(n)s",
                columns=ChIdentifiers(("id", "na me")),
                db=ChIdentifier("we`ird"),
                t=ChIdentifier("t"),
                s="o'neil",
                n=7,
            )
            .build()
        )
        final, _ = bind_query(query.text, query.params, None)

        assert final == (
            "select `id`, `na me` from `we\\`ird`.`t` where s = 'o\\'neil' and n = 7"
        )
