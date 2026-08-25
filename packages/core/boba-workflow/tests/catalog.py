"""Каталог инструментов для тестов домена."""

from __future__ import annotations

from boba.access import ToolAvailability
from boba.workflow import (
    PortDirection,
    ToolArg,
    ToolCatalog,
    ToolFacts,
    ToolPort,
)


def catalog() -> ToolCatalog:
    return {
        "bash": ToolFacts(
            name="bash",
            availability=ToolAvailability.AVAILABLE,
            args=(ToolArg(name="command", required=True), ToolArg(name="stdin")),
            task_ports=True,
        ),
        "pg_query": ToolFacts(
            name="pg_query",
            availability=ToolAvailability.AVAILABLE,
            args=(ToolArg(name="query", required=True), ToolArg(name="limit")),
        ),
        "ch_query": ToolFacts(
            name="ch_query",
            availability=ToolAvailability.AVAILABLE,
            args=(ToolArg(name="query", required=True),),
        ),
        "pg_copy_out": ToolFacts(
            name="pg_copy_out",
            availability=ToolAvailability.AVAILABLE,
            args=(ToolArg(name="query", required=True),),
            ports=(ToolPort(name="out", direction=PortDirection.WRITE),),
        ),
        "ch_insert": ToolFacts(
            name="ch_insert",
            availability=ToolAvailability.AVAILABLE,
            args=(ToolArg(name="table", required=True),),
            ports=(ToolPort(name="src", direction=PortDirection.READ),),
        ),
        "canvas_open": ToolFacts(
            name="canvas_open",
            availability=ToolAvailability.CHAT_ONLY,
            args=(ToolArg(name="path", required=True),),
        ),
        "secret_tool": ToolFacts(
            name="secret_tool",
            availability=ToolAvailability.DENIED,
        ),
    }
