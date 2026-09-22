"""Сборщик запросов Oracle: идентификаторы в кавычках, фрагменты как есть,
значения bind-параметрами, условные куски только при истинном условии."""

from __future__ import annotations

import pytest

from boba.db.oracle.query import OraIdentifier, OraQueryBuilder, OraSql
from boba.toolkit.sql import QueryBuildError


class TestOraQueryBuilder:
    def test_identifier_is_quoted_and_value_stays_a_bind(self) -> None:
        query = (
            OraQueryBuilder()
            .add(
                "select * from {owner}.{table}",
                owner=OraIdentifier("SYS"),
                table=OraIdentifier("obj$"),
            )
            .add("where name = :name", name="o'neil")
            .build()
        )

        assert query.text == 'select * from "SYS"."obj$"\nwhere name = :name'
        assert query.params == {"name": "o'neil"}

    def test_fragment_is_pasted_as_is(self) -> None:
        scope = OraSql("o.owner# in (select u.user# from sys.user$ u)")
        query = (
            OraQueryBuilder()
            .add("select o.obj# from sys.obj$ o where {scope}", scope=scope)
            .build()
        )

        assert query.text == (
            "select o.obj# from sys.obj$ o where "
            "o.owner# in (select u.user# from sys.user$ u)"
        )
        assert query.params is None

    def test_false_condition_leaves_the_piece_out(self) -> None:
        query = (
            OraQueryBuilder()
            .add("select 1 from dual where 1 = 1")
            .when(False, "and a = :a", a=1)
            .when(True, "and b = :b", b=2)
            .build()
        )

        assert query.text == "select 1 from dual where 1 = 1\nand b = :b"
        assert query.params == {"b": 2}

    def test_literal_braces_are_doubled(self) -> None:
        query = OraQueryBuilder().add("select '{{}}' from dual").build()

        assert query.text == "select '{}' from dual"

    def test_same_parameter_with_another_value_is_refused(self) -> None:
        builder = OraQueryBuilder().add("where a = :a", a=1)

        with pytest.raises(QueryBuildError, match="bound twice"):
            builder.add("or b = :a", a=2)

    def test_unknown_placeholder_is_refused(self) -> None:
        with pytest.raises(QueryBuildError, match="expects only names none"):
            OraQueryBuilder().add("select {scope} from dual")

    def test_identifier_with_a_quote_is_refused(self) -> None:
        with pytest.raises(QueryBuildError, match="quotes and NUL"):
            OraQueryBuilder().add("{t}", t=OraIdentifier('a"b'))
