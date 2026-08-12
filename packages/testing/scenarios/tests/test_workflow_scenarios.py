"""Сценарии графа на боевых узлах: postgres и bash соединены рёбрами.

LLM в сценарии не участвует — спека графа собирается руками, продукт листа
приезжает байтами канала данных, побочные эффекты сверяются на живом стенде.
"""

from __future__ import annotations

from boba.stand.flow import FlowStand, SandboxMarks
from boba.stand.pg import PgNodes, PgStand, StandRow, StandTable
from boba.stand.shell import BashNodes
from boba.tool.pg.protocol import PgCopyTrailer
from boba.toolkit.sql import SqlQueryTrailer
from boba.toolkit.workflow import EdgeSpec


@SandboxMarks.NEEDS_SANDBOX
@SandboxMarks.NEEDS_USERNS
class TestPostgresScenarios:
    """Выгрузка, заливка и запрос строк живой базы в графе стадий."""

    SEED = (StandRow(id=1, name="Ivan"), StandRow(id=2, name="Анна"))

    def test_copy_to_stdout_streams_the_table(
        self, pg_stand: PgStand, flow: FlowStand
    ) -> None:
        pg_stand.fill(StandTable.SOURCE, self.SEED)

        run = flow.run([PgNodes.copy_out("dump", StandTable.SOURCE)])

        assert run.exit_code("dump") == 0
        assert run.text("dump") == "1,Ivan\n2,Анна\n"

        trailer = run.outcome.trailer("dump", PgCopyTrailer)
        assert trailer.rows == len(self.SEED)

    def test_query_rows_reach_the_leaf_as_records(
        self, pg_stand: PgStand, flow: FlowStand
    ) -> None:
        """Продукт pg_query — строчный поток: сверяются сами записи, не их счёт."""
        pg_stand.fill(StandTable.SOURCE, self.SEED)

        run = flow.run([PgNodes.query("rows", StandTable.SOURCE.select_rows())])

        rows: list[StandRow] = []
        for record in run.rows("rows"):
            rows.append(StandRow.model_validate(record))

        assert rows == list(self.SEED)

        trailer = run.outcome.trailer("rows", SqlQueryTrailer)
        assert trailer.returns_rows is True
        assert trailer.truncated is False

    def test_bash_literal_loads_the_sink(
        self, pg_stand: PgStand, flow: FlowStand
    ) -> None:
        run = flow.run(
            [
                BashNodes.literal("gen", "cat", "7,Sergey\n"),
                PgNodes.copy_in("load", StandTable.SINK),
            ],
            [EdgeSpec(src="gen", dst="load")],
        )

        assert run.outcome.trailer("load", PgCopyTrailer).rows == 1
        assert list(pg_stand.rows(StandTable.SINK)) == [StandRow(id=7, name="Sergey")]

    def test_query_rows_reach_bash(self, pg_stand: PgStand, flow: FlowStand) -> None:
        pg_stand.fill(StandTable.SOURCE, self.SEED)

        run = flow.run(
            [
                PgNodes.query("rows", StandTable.SOURCE.select_rows()),
                BashNodes.run("names", "grep -o -E '\"name\": \"[^\"]+\"'"),
            ],
            [EdgeSpec(src="rows", dst="names")],
        )

        assert run.text("names") == '"name": "Ivan"\n"name": "Анна"\n'


@SandboxMarks.NEEDS_SANDBOX
@SandboxMarks.NEEDS_USERNS
class TestChainScenarios:
    """Цепочка из трёх боевых узлов: база -> bash -> база и база -> файл."""

    SEED = (StandRow(id=1, name="ivan"), StandRow(id=2, name="boris"))

    def test_table_travels_through_bash_into_the_sink(
        self, pg_stand: PgStand, flow: FlowStand
    ) -> None:
        pg_stand.fill(StandTable.SOURCE, self.SEED)

        run = flow.run(
            [
                PgNodes.copy_out("dump", StandTable.SOURCE),
                BashNodes.run("upper", "tr a-z A-Z"),
                PgNodes.copy_in("load", StandTable.SINK),
            ],
            [
                EdgeSpec(src="dump", dst="upper"),
                EdgeSpec(src="upper", dst="load"),
            ],
        )

        assert run.outcome.trailer("load", PgCopyTrailer).rows == len(self.SEED)
        assert list(pg_stand.rows(StandTable.SINK)) == [
            StandRow(id=1, name="IVAN"),
            StandRow(id=2, name="BORIS"),
        ]

    def test_stream_is_saved_into_the_workspace(
        self, pg_stand: PgStand, flow: FlowStand
    ) -> None:
        pg_stand.fill(StandTable.SOURCE, self.SEED)

        target = flow.guest_path("dump.csv")

        run = flow.run(
            [
                PgNodes.copy_out("dump", StandTable.SOURCE),
                BashNodes.run("save", f"cat > {target}"),
            ],
            [EdgeSpec(src="dump", dst="save")],
        )

        assert run.exit_code("save") == 0
        assert flow.workspace_text("dump.csv") == "1,ivan\n2,boris\n"
