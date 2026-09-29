"""Шаблон create table: главный — шаблон вызывающего, приёмник подставляет
только названные в нём переменные; кусок в квадратных скобках выпадает без
значения, литеральные скобки удваиваются. Чистый разбор строки, сервер не
нужен."""

from __future__ import annotations

from typing import ClassVar

import pytest

from boba.toolkit.transfer import (
    CreateTemplate,
    TemplateVar,
    TemplateVars,
    TransferError,
)

CLICKHOUSE = TemplateVars(
    offered=(
        TemplateVar.DATABASE,
        TemplateVar.TABLE_NAME,
        TemplateVar.COLUMNS,
        TemplateVar.CLUSTER,
        TemplateVar.ORDER_BY,
    )
)


class TestCreateTemplate:
    TEXT: ClassVar[str] = (
        "create table {database}.{table_name}[ on cluster {cluster}] ({columns}) "
        "engine = MergeTree order by {order_by}"
    )
    VALUES: ClassVar[dict[TemplateVar, str]] = {
        TemplateVar.DATABASE: "`db`",
        TemplateVar.TABLE_NAME: "`t`",
        TemplateVar.COLUMNS: "`id` Int64",
        TemplateVar.ORDER_BY: "id",
        TemplateVar.CLUSTER: "",
    }

    def test_optional_part_drops_without_a_value(self) -> None:
        rendered = CreateTemplate(self.TEXT, CLICKHOUSE).render(self.VALUES)

        assert rendered == (
            "create table `db`.`t` (`id` Int64) engine = MergeTree order by id"
        )

    def test_optional_part_renders_with_a_value(self) -> None:
        values = dict(self.VALUES)
        values[TemplateVar.CLUSTER] = "`stand`"
        rendered = CreateTemplate(self.TEXT, CLICKHOUSE).render(values)

        assert rendered == (
            "create table `db`.`t` on cluster `stand` (`id` Int64) "
            "engine = MergeTree order by id"
        )

    def test_literal_brackets_are_doubled(self) -> None:
        text = self.TEXT + " settings x = [[1, 2]] -- {{note}}"
        rendered = CreateTemplate(text, CLICKHOUSE).render(self.VALUES)

        assert rendered.endswith("settings x = [1, 2] -- {note}")

    def test_template_takes_only_the_variables_it_names(self) -> None:
        """Шаблон без кластера и ключа сортировки — такой, как прислала модель."""
        text = "create table {database}.{table_name} ({columns}) engine = Log"
        rendered = CreateTemplate(text, CLICKHOUSE).render(self.VALUES)

        assert rendered == "create table `db`.`t` (`id` Int64) engine = Log"

    def test_variable_outside_brackets_renders_with_a_value(self) -> None:
        text = "create table {database}.{table_name} on cluster {cluster} ({columns})"
        values = dict(self.VALUES)
        values[TemplateVar.CLUSTER] = "`stand`"
        rendered = CreateTemplate(text, CLICKHOUSE).render(values)

        assert rendered == "create table `db`.`t` on cluster `stand` (`id` Int64)"

    def test_variable_outside_brackets_without_a_value_is_refused(self) -> None:
        text = "create table {database}.{table_name} on cluster {cluster} ({columns})"
        template = CreateTemplate(text, CLICKHOUSE)

        with pytest.raises(TransferError, match="no cluster is passed"):
            template.render(self.VALUES)

    @pytest.mark.parametrize(
        ("text", "match"),
        [
            (
                "create table {database}.{table_name}[ on cluster {cluster} "
                "({columns}) order by {order_by}",
                "not closed",
            ),
            (
                "create table {database}.{table_name}[ on cluster [{cluster}]] "
                "({columns}) order by {order_by}",
                "opened inside another",
            ),
            (
                "create table {database}.{table_name}[ on cluster {cluster}] "
                "({columns}) order by {order_by} [settings x = 1]",
                "names no variable",
            ),
            (
                "create table {schema_name}.{table_name}[ on cluster {cluster}] "
                "({columns}) order by {order_by}",
                "uses \\{schema_name\\}, but the receiver has no such value",
            ),
        ],
    )
    def test_broken_template_is_refused(self, text: str, match: str) -> None:
        with pytest.raises(TransferError, match=match):
            CreateTemplate(text, CLICKHOUSE)
