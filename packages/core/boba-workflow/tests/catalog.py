"""Каталог инструментов для тестов домена."""

from __future__ import annotations

from boba.access import ToolAvailability
from boba.toolkit.calls import StudioField
from boba.workflow import (
    PortDirection,
    ToolCatalog,
    ToolFacts,
    ToolPort,
)


def catalog() -> ToolCatalog:
    return {
        "bash": ToolFacts(
            name="bash",
            availability=ToolAvailability.AVAILABLE,
            args=(
                StudioField(name="command", required=True),
                StudioField(name="stdin"),
            ),
            task_ports=True,
        ),
        "pg_query": ToolFacts(
            name="pg_query",
            availability=ToolAvailability.AVAILABLE,
            args=(StudioField(name="query", required=True), StudioField(name="limit")),
        ),
        "ch_query": ToolFacts(
            name="ch_query",
            availability=ToolAvailability.AVAILABLE,
            args=(StudioField(name="query", required=True),),
        ),
        "pg_copy_out": ToolFacts(
            name="pg_copy_out",
            availability=ToolAvailability.AVAILABLE,
            args=(StudioField(name="query", required=True),),
            ports=(ToolPort(name="out", direction=PortDirection.WRITE),),
        ),
        "ch_insert": ToolFacts(
            name="ch_insert",
            availability=ToolAvailability.AVAILABLE,
            args=(StudioField(name="table", required=True),),
            ports=(ToolPort(name="src", direction=PortDirection.READ),),
        ),
        "secret_tool": ToolFacts(
            name="secret_tool",
            availability=ToolAvailability.DENIED,
        ),
    }
