"""Сборщик запросов Oracle: имена квотирует драйвер (простое и составное имя как
есть, иное в двойных кавычках, кавычка внутри отвергается), списки имён,
литералов и bind-меток собираются через запятую, фрагменты идут как есть,
значения остаются bind-параметрами, условные куски только при истинном условии."""

from __future__ import annotations

import pytest

from boba.db.oracle.query import (
    OraBindMarks,
    OraIdentifier,
    OraIdentifiers,
    OraLiterals,
    OraQueryBuilder,
    OraSql,
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
    def test_names_render_and_value_stays_a_bind(self) -> None:
        query = (
            OraQueryBuilder()
            .add(
                "select {columns} from {owner}.{table}",
                columns=OraIdentifiers(["name", "obj#"]),
                owner=OraIdentifier("SYS"),
                table=OraIdentifier("obj$"),
            )
            .add(
                "where name = :name and type in ({kinds})",
                name="o'neil",
                kinds=OraLiterals(["TABLE"]),
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
            OraQueryBuilder(
                table=OraIdentifier("HR.EMPLOYEES"),
                columns=OraIdentifiers(columns),
                binds=OraBindMarks(len(columns)),
            )
            .add("insert into {table} ({columns}) values ({binds})")
            .build()
        )

        assert query.text == 'insert into HR.EMPLOYEES (ID, "na me") values (:1, :2)'
        assert query.params is None

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
