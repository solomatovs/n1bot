"""Общие SQL-ресурсы скрапера и выбор вариантов PostgreSQL/Greenplum."""

from importlib.resources import files

import pytest

from boba.db.postgres.connection import (
    PostgresConfig,
    PostgresOptionsConfig,
    PostgresPoolConfig,
)
from boba.db.postgres.connection.auth import TrustAuth
from boba.ix_core.scrape import ScrapeFile
from boba.pg_meta_scraper.worker import PgSource, WorkerConfig


@pytest.fixture
def source() -> PgSource:
    profile = PostgresConfig(
        host="example.test",
        port=5432,
        dbname="example",
        auth=TrustAuth(method="trust", user="scraper"),
        options=PostgresOptionsConfig(),
        pool=PostgresPoolConfig(),
    )

    return PgSource(WorkerConfig(source=profile))


def test_all_query_and_verification_resources_exist(source: PgSource) -> None:
    directory = files("boba.pg_meta_scraper") / "scrape"

    for query in source.files:
        assert (directory / query.query).is_file(), query.query
        assert (directory / query.verify()).is_file(), query.verify()


@pytest.mark.parametrize(
    ("version", "flavor", "expected"),
    [
        (90000, "", "2_class__lt91.sql"),
        (90100, "", "2_class__91_96.sql"),
        (90600, "", "2_class__91_96.sql"),
        (100000, "", "2_class.sql"),
        (190000, "", "2_class.sql"),
        (90400, "gp", "2_class__gp6.sql"),
        (120000, "gp", "2_class.sql"),
    ],
)
def test_class_variants_keep_version_and_flavor_gates(
    source: PgSource, version: int, flavor: str, expected: str
) -> None:
    chosen = []
    for query in source.files:
        if query.name != "class":
            continue

        if query.applies((version,), flavor):
            chosen.append(query)

    assert len(chosen) == 1
    assert chosen[0].query == expected
    assert chosen[0].verify() == "2_class.verify.sql"


def test_verification_uses_sibling_unless_an_explicit_query_is_given() -> None:
    default = ScrapeFile(name="catalog", wave=1, query="1_catalog.sql")
    shared = ScrapeFile(
        name="catalog", wave=1, query="1_catalog.sql", verify_query="1_catalog.sql"
    )

    assert default.verify() == "1_catalog.verify.sql"
    assert shared.verify() == "1_catalog.sql"
