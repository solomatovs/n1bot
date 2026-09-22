"""Сборщик запросов ch: текст с серверными параметрами {name:Type} склеивается
кусками, значения уезжают словарём, условные куски попадают только при истинном
условии."""

from __future__ import annotations

import pytest

from boba.db.clickhouse.query import ChQueryBuilder
from boba.toolkit.sql import QueryBuildError


class TestChQueryBuilder:
    def test_pieces_join_and_values_stay_parameters(self) -> None:
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
            .add("where a = {a:UInt8}", a=1)
            .add("or b = {a:UInt8}", a=1)
            .build()
        )

        assert query.params == {"a": 1}

    def test_same_parameter_with_another_value_is_refused(self) -> None:
        builder = ChQueryBuilder().add("where a = {a:UInt8}", a=1)

        with pytest.raises(QueryBuildError, match="bound twice"):
            builder.add("or b = {a:UInt8}", a=2)
