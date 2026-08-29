"""Секция [cluster]: имя узла обязательно, heartbeat в ttl, имена инстансов."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from boba.runtime.config import AppName, ClusterConfig


def _cluster(**overrides: object) -> ClusterConfig:
    values: dict[str, object] = {
        "node_id": "node1",
        "db_schema": "live",
        "host": "host-a",
        "lock_ttl_sec": 20,
        "heartbeat_sec": 6,
        "reaper_period_sec": 10,
        "queue_usage_limit": 0.5,
        "retention_sec": 3600,
    }
    values.update(overrides)
    return ClusterConfig.model_validate(values)


def test_instance_names_differ_per_application() -> None:
    cluster = _cluster()

    assert cluster.instance_of(AppName.CHAINLIT) == "node1-chainlit"
    assert cluster.instance_of(AppName.STUDIO) == "node1-studio"


def test_node_id_is_required_and_safe() -> None:
    with pytest.raises(ValidationError, match="node_id"):
        _cluster(node_id="")

    with pytest.raises(ValidationError, match="node_id"):
        _cluster(node_id="node 1")


def test_heartbeat_must_fit_into_the_ttl() -> None:
    with pytest.raises(ValidationError, match="half"):
        _cluster(lock_ttl_sec=10, heartbeat_sec=6)
