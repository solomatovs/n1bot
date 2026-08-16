"""Повторный load_tools: модульные TOOLS-синглтоны остаются нетронутыми.

Загрузчик зовётся не один раз за процесс (bootstrap, DI-провайдер на сессию);
обвязки обязаны ставиться на копии — иначе второй проход видит уже пришитое
поле call_id и падает в резолвере injected-конфига.
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from boba.chainlit.agent.toolrun.call_id import ToolCallIdField
from boba.chainlit.infra.plugins import load_tools
from boba.tool.pg.tools import TOOLS as PG_TOOLS
from boba.tool.pg.tools import pg_list_targets


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    pass


def _schema_fields(tool: object) -> set[str]:
    schema = getattr(tool, "args_schema", None)
    assert isinstance(schema, type)
    assert issubclass(schema, BaseModel)
    return set(schema.model_fields)


def test_repeated_load_serves_wrapped_copies(raw_config) -> None:
    first = load_tools(raw_config)
    second = load_tools(raw_config)

    assert [t.name for t in first.tools] == [t.name for t in second.tools]

    by_name = {t.name: t for t in second.tools}
    loaded = by_name["pg_list_targets"]

    assert ToolCallIdField.NAME in _schema_fields(loaded)
    assert "cfg" not in _schema_fields(loaded)


def test_module_singletons_stay_pristine(raw_config) -> None:
    load_tools(raw_config)
    load_tools(raw_config)

    for tool in PG_TOOLS:
        fields = _schema_fields(tool)

        assert ToolCallIdField.NAME not in fields
        assert "cfg" in fields

    origin = ToolMainBody.of(pg_list_targets)
    assert origin.__module__ == "boba.tool.pg.tools"


class ToolMainBody:
    """Достаёт тело инструмента: у нетронутого синглтона это функция модуля."""

    @staticmethod
    def of(tool: object) -> object:
        body = getattr(tool, "coroutine", None)
        if body is None:
            body = getattr(tool, "func", None)

        assert body is not None
        return body
