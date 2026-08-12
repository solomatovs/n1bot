"""Потоковые сценарии графа: сырые байты между боевыми узлами postgres и bash.

Объём набора выведен из буфера ребра насоса (StandData.edge_floor), поэтому
стадии работают одновременно, а не обмениваются заранее готовым буфером;
байты и строки сверяются точно.
"""

from __future__ import annotations

from enum import StrEnum
from typing import ClassVar

from boba.stand.flow import FlowStand, SandboxMarks
from boba.stand.pg import PgNodes, PgStand, StandCsv, StandData, StandTable
from boba.stand.shell import BashNodes
from boba.tool.pg.protocol import PgCopyTrailer
from boba.toolkit.channels import ShellExit
from boba.toolkit.payload import FailureKind, PayloadExit
from boba.toolkit.workflow import EdgeSpec


class FlowFile(StrEnum):
    """Файлы сценариев в /workspace: вход графа и снимки его потока."""

    INPUT = "input.csv"
    DUMP = "dump.csv"
    COPY = "copy.csv"
    BROKEN = "broken.csv"


@SandboxMarks.NEEDS_SANDBOX
@SandboxMarks.NEEDS_USERNS
class TestStreamScenarios:
    """Поток из живой базы и в живую базу: файл, веер, фильтр и отказ."""

    BAD_INTEGER: ClassVar[str] = "SQLSTATE 22P02"
    """Код postgres на нечисловой id: он и объясняет отказ приёмника."""

    HOLD_SEC: ClassVar[int] = 30
    """Длительность соседней ветки: отказ обязан снять её задолго до конца."""

    def test_file_stream_loads_the_sink(
        self, pg_stand: PgStand, flow: FlowStand
    ) -> None:
        rows = StandData.mixed()
        path = flow.workspace_write(FlowFile.INPUT, StandCsv.render(rows))

        run = flow.run(
            [
                BashNodes.run("gen", f"cat {path}"),
                PgNodes.copy_in("load", StandTable.SINK),
            ],
            [EdgeSpec(src="gen", dst="load")],
        )

        assert run.outcome.trailer("load", PgCopyTrailer).rows == len(rows)
        assert list(pg_stand.rows(StandTable.SINK)) == list(rows)

    def test_table_stream_reaches_the_file_byte_for_byte(
        self, pg_stand: PgStand, flow: FlowStand
    ) -> None:
        rows = StandData.mixed()
        pg_stand.fill(StandTable.SOURCE, rows)

        target = flow.guest_path(FlowFile.DUMP)

        run = flow.run(
            [
                PgNodes.copy_out("dump", StandTable.SOURCE),
                BashNodes.run("save", f"cat > {target}"),
            ],
            [EdgeSpec(src="dump", dst="save")],
        )

        assert run.exit_code("save") == 0
        assert flow.workspace_bytes(FlowFile.DUMP) == StandCsv.render(rows)

    def test_table_travels_into_the_sink_unparsed(
        self, pg_stand: PgStand, flow: FlowStand
    ) -> None:
        rows = StandData.mixed()
        pg_stand.fill(StandTable.SOURCE, rows)

        run = flow.run(
            [
                PgNodes.copy_out("dump", StandTable.SOURCE),
                PgNodes.copy_in("load", StandTable.SINK),
            ],
            [EdgeSpec(src="dump", dst="load")],
        )

        assert run.outcome.trailer("dump", PgCopyTrailer).rows == len(rows)
        assert run.outcome.trailer("load", PgCopyTrailer).rows == len(rows)
        assert list(pg_stand.rows(StandTable.SINK)) == list(rows)

    def test_fanout_feeds_the_sink_and_the_file(
        self, pg_stand: PgStand, flow: FlowStand
    ) -> None:
        rows = StandData.mixed()
        pg_stand.fill(StandTable.SOURCE, rows)

        target = flow.guest_path(FlowFile.COPY)

        run = flow.run(
            [
                PgNodes.copy_out("dump", StandTable.SOURCE),
                PgNodes.copy_in("load", StandTable.SINK),
                BashNodes.run("save", f"cat > {target}"),
            ],
            [
                EdgeSpec(src="dump", dst="load"),
                EdgeSpec(src="dump", dst="save"),
            ],
        )

        assert run.exit_code("save") == 0
        assert run.outcome.trailer("load", PgCopyTrailer).rows == len(rows)
        assert list(pg_stand.rows(StandTable.SINK)) == list(rows)
        assert flow.workspace_bytes(FlowFile.COPY) == StandCsv.render(rows)

    def test_filter_between_tables_keeps_marked_rows(
        self, pg_stand: PgStand, flow: FlowStand
    ) -> None:
        source = StandData.bulk()
        kept = StandData.kept()

        assert 0 < len(kept) < len(source)

        pg_stand.fill(StandTable.SOURCE, source)

        run = flow.run(
            [
                PgNodes.copy_out("dump", StandTable.SOURCE),
                BashNodes.run("filter", f"grep -F -- {StandData.KEEP}"),
                PgNodes.copy_in("load", StandTable.SINK),
            ],
            [
                EdgeSpec(src="dump", dst="filter"),
                EdgeSpec(src="filter", dst="load"),
            ],
        )

        assert run.outcome.trailer("load", PgCopyTrailer).rows == len(kept)
        assert list(pg_stand.rows(StandTable.SINK)) == list(kept)

    def test_broken_row_fails_the_graph_and_kills_neighbours(
        self, pg_stand: PgStand, flow: FlowStand
    ) -> None:
        """Приёмник падает на битой строке: соседняя ветка снята, таблица пуста.

        Источник заливки доживает до конца сам: сорванный COPY отбрасывает
        остаток входа на сервере, поэтому приёмник узнаёт об отказе последним.
        """
        path = flow.workspace_write(FlowFile.BROKEN, StandData.broken_csv())

        failure = flow.failed(
            [
                BashNodes.run("gen", f"cat {path}"),
                PgNodes.copy_in("load", StandTable.SINK),
                PgNodes.query("slow", f"SELECT pg_sleep({self.HOLD_SEC})"),
            ],
            [EdgeSpec(src="gen", dst="load")],
        )

        assert failure.kind == FailureKind.SQL_FAILED
        assert self.BAD_INTEGER in failure.message
        assert failure.exit_code("load") == PayloadExit.FAILURE
        assert failure.killed("load") is False

        assert failure.exit_code("gen") == 0

        assert failure.killed("slow") is True
        assert failure.exit_code("slow") == ShellExit.KILLED

        assert list(pg_stand.rows(StandTable.SINK)) == []
