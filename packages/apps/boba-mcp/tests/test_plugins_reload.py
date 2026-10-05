"""Повторный StandRefs.registry: модульные TOOLS-синглтоны остаются нетронутыми.

Загрузчик зовётся не один раз за процесс (bootstrap, DI-провайдер на сессию);
обвязки обязаны ставиться на копии — иначе второй проход видит уже пришитое
поле call_id и падает в резолвере injected-конфига.
"""

from __future__ import annotations

import pytest
from omegaconf import DictConfig, OmegaConf
from pydantic import BaseModel

from boba.stand.refs import StandRefs
from boba.stand_core.context import CallStand
from boba.tool.pg.tools import TOOLS as PG_TOOLS
from boba.tool.pg.tools import pg_query
from boba.toolrun.call_id import CallFields


@pytest.fixture
def reload_config(raw_config: DictConfig) -> DictConfig:
    """Конфиг с процессным запуском: тест про обвязку tools, не про песочницу."""
    copied = raw_config.copy()
    OmegaConf.update(copied, "env.tool_launcher", "process")
    return copied


def _schema_fields(tool: object) -> set[str]:
    schema = getattr(tool, "args_schema", None)
    if not (isinstance(schema, type)):
        raise AssertionError("isinstance(schema, type)")
    if not (issubclass(schema, BaseModel)):
        raise AssertionError("issubclass(schema, BaseModel)")
    return set(schema.model_fields)


def test_repeated_load_serves_wrapped_copies(
    runtime_stand: StandRefs, call_stand: CallStand, reload_config: DictConfig
) -> None:
    first = runtime_stand.registry(
        reload_config, runtime_stand.none(), StandRefs.granted("all", ["*"])
    )
    second = runtime_stand.registry(
        reload_config, runtime_stand.none(), StandRefs.granted("all", ["*"])
    )

    if [t.name for t in first.tools] != [t.name for t in second.tools]:
        raise AssertionError("[t.name for t in first.tools] == [t.name for t in secon…")

    by_name = {t.name: t for t in second.tools}
    loaded = by_name["pg_query"]

    if CallFields.CALL_ID not in _schema_fields(loaded):
        raise AssertionError("CallFields.CALL_ID in _schema_fields(loaded)")
    if "cfg" in _schema_fields(loaded):
        raise AssertionError('"cfg" not in _schema_fields(loaded)')


def test_module_singletons_stay_pristine(
    runtime_stand: StandRefs, call_stand: CallStand, reload_config: DictConfig
) -> None:
    runtime_stand.registry(
        reload_config, runtime_stand.none(), StandRefs.granted("all", ["*"])
    )
    runtime_stand.registry(
        reload_config, runtime_stand.none(), StandRefs.granted("all", ["*"])
    )

    for tool in PG_TOOLS:
        if CallFields.CALL_ID not in _schema_fields(tool):
            continue

        raise AssertionError(f"{tool.name}: обвязка пришила call_id синглтону")

    origin = ToolMainBody.of(pg_query)
    if origin.__module__ != "boba.tool.pg.tools":
        raise AssertionError('origin.__module__ == "boba.tool.pg.tools"')


class ToolMainBody:
    """Достаёт тело инструмента: у нетронутого синглтона это функция модуля."""

    @staticmethod
    def of(tool: object) -> object:
        body = getattr(tool, "coroutine", None)
        if body is None:
            body = getattr(tool, "func", None)

        if body is None:
            raise AssertionError("body is not None")
        return body
