"""Фикстуры домена каталога: образцы источников и процесса; стенд не нужен."""

from __future__ import annotations

import pytest

from boba.catalog import (
    CatalogSnapshot,
    SnapshotResolver,
)
from boba.catalog.samples import ProcessSample, SampleIds
from boba.db.postgres.snapshot_sample import PgSample


@pytest.fixture(scope="session")
def kerberos_workspace() -> None:
    """Домен без I/O: стенд и kerberos тестам не нужны."""


@pytest.fixture
def pg() -> PgSample:
    return PgSample()


@pytest.fixture
def process() -> ProcessSample:
    return ProcessSample()


@pytest.fixture
def snapshot(process: ProcessSample) -> CatalogSnapshot:
    return process.snapshot()


@pytest.fixture
def resolver(pg: PgSample) -> SnapshotResolver:
    return SnapshotResolver({SampleIds.POSTGRES: pg.snapshot()})
