"""Сборщик запросов Oracle: имена квотирует драйвер (простое и составное имя как
есть, иное в двойных кавычках, кавычка внутри отвергается), списки имён,
литералов и bind-меток собираются через запятую, куски склеиваются подряд без
разбора текста, значения остаются bind-параметрами, условные куски только при
истинном условии."""

from __future__ import annotations

import pytest

from boba.db.oracle.query import (
    OraBindMarks,
    OraIdentifier,
    OraIdentifiers,
    OraLiterals,
    OraQueryBuilder,
)
from boba.toolkit.sql import QueryBuildError


class TestOraIdentifier:
    @pytest.mark.parametrize(
        "name", ["SYS", "obj$", "sys.obj$", "hr.employees", "x@link", '"Mixed".t']
    )
    def test_qualified_name_goes_as_is(self, name: str) -> None:
        assert OraIdentifier(name).render() == name

    @pytest.mark.parametrize(
        ("name", "quoted"),
        [("we ird", '"we ird"'), ("1abc", '"1abc"'), ("a-b", '"a-b"')],
    )
    def test_other_name_is_quoted_verbatim(self, name: str, quoted: str) -> None:
        assert OraIdentifier(name).render() == quoted

    def test_embedded_quote_is_refused_by_the_driver(self) -> None:
        with pytest.raises(QueryBuildError, match="cannot be quoted"):
            OraIdentifier('a"b').render()

    @pytest.mark.parametrize("name", ["", "  "])
    def test_empty_name_is_refused(self, name: str) -> None:
        with pytest.raises(QueryBuildError, match="non-empty"):
            OraIdentifier(name)


class TestLists:
    def test_identifiers_join_with_a_comma(self) -> None:
        assert OraIdentifiers(["ID", "na me"]).render() == 'ID, "na me"'

    def test_literals_are_quoted_by_the_driver(self) -> None:
        assert OraLiterals(["TABLE", "o'neil"]).render() == "'TABLE', 'o''neil'"

    def test_bind_marks_count_from_one(self) -> None:
        assert OraBindMarks(3).render() == ":1, :2, :3"

    def test_empty_lists_are_refused(self) -> None:
        with pytest.raises(QueryBuildError, match="at least one name"):
            OraIdentifiers([])

        with pytest.raises(QueryBuildError, match="at least one value"):
            OraLiterals([])

        with pytest.raises(QueryBuildError, match="positive count"):
            OraBindMarks(0)


class TestOraQueryBuilder:
    def test_pieces_join_and_value_stays_a_bind(self) -> None:
        query = (
            OraQueryBuilder()
            .add(
                "select ",
                OraIdentifiers(["name", "obj#"]),
                " from ",
                OraIdentifier("SYS"),
                ".",
                OraIdentifier("obj$"),
            )
            .add(
                "where name = :name and type in (",
                OraLiterals(["TABLE"]),
                ")",
                name="o'neil",
            )
            .build()
        )

        assert query.text == (
            "select name, obj# from SYS.obj$\nwhere name = :name and type in ('TABLE')"
        )
        assert query.params == {"name": "o'neil"}

    def test_insert_marks_follow_the_columns(self) -> None:
        columns = ["ID", "na me"]
        query = (
            OraQueryBuilder()
            .add(
                "insert into ",
                OraIdentifier("HR.EMPLOYEES"),
                " (",
                OraIdentifiers(columns),
                ") values (",
                OraBindMarks(len(columns)),
                ")",
            )
            .build()
        )

        assert query.text == 'insert into HR.EMPLOYEES (ID, "na me") values (:1, :2)'
        assert query.params is None

    def test_text_is_not_parsed(self) -> None:
        query = (
            OraQueryBuilder()
            .add("select '{}', '$x', '%(y)s', json_object('a' value 1) from dual")
            .build()
        )

        assert query.text == (
            "select '{}', '$x', '%(y)s', json_object('a' value 1) from dual"
        )

    def test_false_condition_leaves_the_piece_out(self) -> None:
        query = (
            OraQueryBuilder()
            .add("select 1 from dual where 1 = 1")
            .when(False, "and a = :a", a=1)
            .when(True, "and b = ", OraIdentifier("b"), b=2)
            .build()
        )

        assert query.text == "select 1 from dual where 1 = 1\nand b = b"
        assert query.params == {"b": 2}

    def test_same_parameter_with_another_value_is_refused(self) -> None:
        builder = OraQueryBuilder().add("where a = :a", a=1)

        with pytest.raises(QueryBuildError, match="bound twice"):
            builder.add("or b = :a", a=2)

    def test_name_in_bind_is_refused(self) -> None:
        with pytest.raises(QueryBuildError, match="pieces go positionally"):
            OraQueryBuilder().add("select 1 from dual", t=OraIdentifier("x"))

    def test_built_query_in_bind_is_refused(self) -> None:
        inner = OraQueryBuilder().add("select 1 from dual").build()

        with pytest.raises(QueryBuildError, match="built query"):
            OraQueryBuilder().add("select * from (:q)", q=inner)
