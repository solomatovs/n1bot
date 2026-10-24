"""Имена стенда на общих серверах: метка разработчика из конфига стенда и
предел длины имени postgres."""

from __future__ import annotations

import pytest

from boba.stand.names import StandNames
from boba.stand.site import Stand, StandError


def test_names_carry_the_developer_of_the_stand_config() -> None:
    developer = Stand.load().developer
    names = StandNames()

    assert names.of("probe").startswith(f"{developer}_probe")
    assert names.of_tree("probe").startswith(f"{developer}_probe")
    assert names.database("probe").startswith(f"aid_{developer}_probe")
    assert names.tree_database("probe").startswith(f"aid_{developer}_probe")


def test_tree_label_ends_the_tree_database_name(request: pytest.FixtureRequest) -> None:
    tree = request.config.rootpath.name.lower().replace("-", "_")

    assert StandNames().tree_database("probe").endswith(f"_{tree}")


def test_name_longer_than_postgres_keeps_is_refused() -> None:
    with pytest.raises(StandError, match="postgres keeps 63"):
        StandNames().database("x" * 60)
