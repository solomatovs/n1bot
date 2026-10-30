"""Повторный StandRefs.registry: каждая сборка ставит обвязки на свежие
инструменты хоста из объявлений манифеста.

Загрузчик зовётся не один раз за процесс (bootstrap, DI-провайдер на сессию);
вторая сборка обязана получить те же инструменты с той же схемой: поля хоста
ставятся заново, а служебные параметры тела в схему не попадают.
"""

from __future__ import annotations

import pytest
from omegaconf import DictConfig

from boba.stand.overlay import ConfigOverlay
from boba.stand.refs import StandRefs
from boba.stand_core.context import CallStand
from boba.toolkit.calls import ToolIntent
from boba.toolrun.hosted import ToolSchema

pytestmark = pytest.mark.anyio


@pytest.fixture
def reload_config(raw_config: DictConfig) -> DictConfig:
    """Конфиг с процессным запуском и одним pg: тест про обвязки, не про
    песочницу и хранилища."""
    changes: dict[str, object] = {"env.tool_launcher": "process"}
    for plugin_id in raw_config["tool"]:
        changes[f"tool.{plugin_id}.enable"] = plugin_id == "pg"

    return ConfigOverlay(raw_config).merged(changes)


async def test_repeated_load_builds_the_same_tools(
    runtime_stand: StandRefs, call_stand: CallStand, reload_config: DictConfig
) -> None:
    first = await runtime_stand.registry(
        reload_config, runtime_stand.none(), StandRefs.granted("all", ["*"])
    )
    second = await runtime_stand.registry(
        reload_config, runtime_stand.none(), StandRefs.granted("all", ["*"])
    )

    if [t.name for t in first.tools] != [t.name for t in second.tools]:
        raise AssertionError("both loads build the same tools in the same order")

    schemas = ToolSchema()
    by_name = {t.name: t for t in second.tools}
    card = schemas.card_of(by_name["pg_query"])
    properties = card.parameters["properties"]

    if ToolIntent.NAME not in properties:
        raise AssertionError(f"the host adds the intent field: {list(properties)}")
    if "cfg" in properties:
        raise AssertionError(f"the plugin config stays off the schema: {properties}")
    if "sql" not in properties:
        raise AssertionError(
            f"the model argument comes from the manifest: {properties}"
        )

    again = schemas.card_of({t.name: t for t in first.tools}["pg_query"])
    if again.parameters != card.parameters:
        raise AssertionError("the second load serves the same call schema")
