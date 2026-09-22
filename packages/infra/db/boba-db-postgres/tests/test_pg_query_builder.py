"""Сборщик запросов pg: идентификаторы квотируются, значения уезжают параметрами,
условные куски попадают в текст только при истинном условии."""

from __future__ import annotations

from pathlib import Path

import pytest
from psycopg import sql

from boba.db.postgres.query import PgQueryBuilder
from boba.toolkit.sql import QueryBuildError


class TestPgQueryBuilder:
    def test_identifier_is_quoted_and_value_stays_a_parameter(self) -> None:
        query = (
            PgQueryBuilder()
            .add("select * from {table}", table=sql.Identifier("dm", "my table"))
            .add("where name = %(name)s", name="o'neil")
            .build()
        )

        text = query.text.as_string()

        assert text == 'select * from "dm"."my table"\nwhere name = %(name)s'
        assert query.params == {"name": "o'neil"}

    def test_false_condition_leaves_the_piece_out(self) -> None:
        query = (
            PgQueryBuilder()
            .add("select 1 where true")
            .when(False, "and a = %(a)s", a=1)
            .when(True, "and b = %(b)s", b=2)
            .build()
        )

        assert query.text.as_string() == "select 1 where true\nand b = %(b)s"
        assert query.params == {"b": 2}

    def test_literal_braces_and_percents_are_doubled(self) -> None:
        query = (
            PgQueryBuilder()
            .add("select '{{}}'::jsonb, 'a%%b' where x like %(x)s", x="k_%")
            .build()
        )

        assert query.text.as_string() == "select '{}'::jsonb, 'a%%b' where x like %(x)s"

    def test_same_parameter_with_the_same_value_is_fine(self) -> None:
        query = (
            PgQueryBuilder()
            .add("where a = %(a)s", a=1)
            .add("or b = %(a)s", a=1)
            .build()
        )

        assert query.params == {"a": 1}

    def test_same_parameter_with_another_value_is_refused(self) -> None:
        builder = PgQueryBuilder().add("where a = %(a)s", a=1)

        with pytest.raises(QueryBuildError, match="bound twice"):
            builder.add("or b = %(a)s", a=2)

    def test_standing_names_apply_to_every_piece(self) -> None:
        query = (
            PgQueryBuilder(schema=sql.Identifier("ix"))
            .add("select 1 from {schema}.node")
            .add("union all select 1 from {schema}.edge where a = %(a)s", a=1)
            .build()
        )

        assert query.text.as_string() == (
            'select 1 from "ix".node\nunion all select 1 from "ix".edge where a = %(a)s'
        )

    def test_standing_name_must_be_a_name(self) -> None:
        with pytest.raises(QueryBuildError, match="standing name 'schema'"):
            PgQueryBuilder(schema="ix")

    def test_read_adds_the_file_as_a_piece(self, tmp_path: Path) -> None:
        path = tmp_path / "10_node.sql"
        path.write_text(
            "select * from {schema}.node where id = %(id)s", encoding="utf-8"
        )

        query = PgQueryBuilder(schema=sql.Identifier("ix")).read(path, id=7).build()

        assert query.text.as_string() == 'select * from "ix".node where id = %(id)s'
        assert query.params == {"id": 7}

    def test_unknown_placeholder_is_refused(self) -> None:
        with pytest.raises(QueryBuildError, match="expects only names schema"):
            PgQueryBuilder().add(
                "select * from {schema}.{table}", schema=sql.Identifier("ix")
            )
