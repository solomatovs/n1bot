"""Реестры пар по entry points: pg_stream_in и ch_stream_in берут пару по
движку источника из кадра schema. Пакеты пар стоят в окружении стенда, и
каждый обязан найтись своим классом; для движка без пары реестр отвечает
отказом с подсказкой взять arrow."""

from __future__ import annotations

import pytest

from boba.db.clickhouse.transfer import ChTransfers
from boba.db.postgres.transfer import PgTransfers
from boba.stream.ch_to_ch.transfer import ChToCh
from boba.stream.ch_to_pg.transfer import ChToPg
from boba.stream.pg_to_ch.transfer import PgToCh
from boba.stream.pg_to_pg.transfer import PgToPg
from boba.toolkit.transfer import TransferError


class TestRegistries:
    def test_postgres_receiver_finds_its_pairs(self) -> None:
        transfers = PgTransfers()

        assert transfers.pair("postgres") is PgToPg
        assert transfers.pair("clickhouse") is ChToPg

    def test_clickhouse_receiver_finds_its_pairs(self) -> None:
        transfers = ChTransfers()

        assert transfers.pair("clickhouse") is ChToCh
        assert transfers.pair("postgres") is PgToCh

    def test_engine_without_a_pair_is_refused(self) -> None:
        with pytest.raises(TransferError, match="no adapter for source 'oracle'"):
            PgTransfers().pair("oracle")
