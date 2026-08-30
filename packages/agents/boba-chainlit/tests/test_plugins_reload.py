"""Повторный ChatPlugins.load: модульные TOOLS-синглтоны остаются нетронутыми.

Загрузчик зовётся не один раз за процесс (bootstrap, DI-провайдер на сессию);
обвязки обязаны ставиться на копии — иначе второй проход видит уже пришитое
поле call_id и падает в резолвере injected-конфига.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from pydantic import BaseModel

from boba.chainlit.infra.plugins import ChatPlugins
from boba.connection_broker.store import ConnectionStore
from boba.sandbox import ZygoteRegistry
from boba.stand.refs import StandRefs
from boba.tool.pg.tools import TOOLS as PG_TOOLS
from boba.tool.pg.tools import pg_connection_list
from boba.toolrun.call_id import ToolCallIdField


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    pass


@pytest.fixture(autouse=True)
def app_sandbox() -> Iterator[None]:
    """Зиготы секций гасятся после теста, как это делает выход приложения."""
    try:
        yield
    finally:
        ZygoteRegistry.stop_all()


def _no_registry() -> None:
    return None


def _no_store() -> ConnectionStore:
    msg = "connection store is not used by the reload test"
    raise RuntimeError(msg)


def _schema_fields(tool: object) -> set[str]:
    schema = getattr(tool, "args_schema", None)
    if not (isinstance(schema, type)):
        raise AssertionError("isinstance(schema, type)")
    if not (issubclass(schema, BaseModel)):
        raise AssertionError("issubclass(schema, BaseModel)")
    return set(schema.model_fields)


def test_repeated_load_serves_wrapped_copies(raw_config) -> None:
    first = ChatPlugins.load(raw_config, StandRefs.of(_no_store, _no_registry))
    second = ChatPlugins.load(raw_config, StandRefs.of(_no_store, _no_registry))

    if [t.name for t in first.tools] != [t.name for t in second.tools]:
        raise AssertionError("[t.name for t in first.tools] == [t.name for t in secon…")

    by_name = {t.name: t for t in second.tools}
    loaded = by_name["pg_connection_list"]

    if ToolCallIdField.NAME not in _schema_fields(loaded):
        raise AssertionError("ToolCallIdField.NAME in _schema_fields(loaded)")
    if "cfg" in _schema_fields(loaded):
        raise AssertionError('"cfg" not in _schema_fields(loaded)')


def test_module_singletons_stay_pristine(raw_config) -> None:
    ChatPlugins.load(raw_config, StandRefs.of(_no_store, _no_registry))
    ChatPlugins.load(raw_config, StandRefs.of(_no_store, _no_registry))

    for tool in PG_TOOLS:
        fields = _schema_fields(tool)

        if ToolCallIdField.NAME in fields:
            raise AssertionError("ToolCallIdField.NAME not in fields")
        if "cfg" not in fields:
            raise AssertionError('"cfg" in fields')

    origin = ToolMainBody.of(pg_connection_list)
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
