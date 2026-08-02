"""Тесты ролевого доступа: нотация конфига, набор под роли, отказ на вызове."""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool

from boba.chainlit2.agent.tools.access import (
    ToolAccess,
    ToolAccessDeniedError,
    ToolAccessGuard,
)
from boba.chainlit2.infra.plugins import PluginMeta, ToolRegistry
from boba.chainlit2.infra.providers import build_llm_view


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    pass


class TestPluginMetaNotation:
    @staticmethod
    def _meta(**kw) -> PluginMeta:
        return PluginMeta(**kw)

    def test_own_roles_win_over_section(self) -> None:
        meta = self._meta(enable=True, roles=["DEV"], tools={"query": ["ADM"]})
        assert meta.roles_of("query") == ["ADM"]

    def test_empty_list_inherits_section_roles(self) -> None:
        meta = self._meta(enable=True, roles=["DEV", "ADM"], tools={"list": []})
        assert meta.roles_of("list") == ["DEV", "ADM"]

    def test_empty_list_without_section_roles_is_deny(self) -> None:
        meta = self._meta(enable=True, tools={"list": []})
        assert meta.roles_of("list") == []

    def test_tools_table_is_the_allowlist(self) -> None:
        meta = self._meta(enable=True, tools={"a": ["ADM"]})
        assert "a" in meta.tools
        assert "b" not in meta.tools


class TestToolAccess:
    ACCESS = ToolAccess({
        "query": ["ADM"],
        "list_targets": ["DEV", "ADM"],
        "visualize": ["*"],
        "forgotten": [],
    })

    def test_matching_role_allowed(self) -> None:
        assert self.ACCESS.allowed("query", {"ADM"}) is True

    def test_other_role_denied(self) -> None:
        assert self.ACCESS.allowed("query", {"DEV"}) is False

    def test_any_intersection_is_enough(self) -> None:
        assert self.ACCESS.allowed("list_targets", {"DEV"}) is True

    def test_wildcard_allows_any_role(self) -> None:
        assert self.ACCESS.allowed("visualize", {"WHATEVER"}) is True

    def test_wildcard_still_needs_a_user(self) -> None:
        assert self.ACCESS.allowed("visualize", set()) is True

    def test_empty_roles_deny_by_default(self) -> None:
        assert self.ACCESS.allowed("forgotten", {"ADM"}) is False

    def test_unknown_tool_denied(self) -> None:
        assert self.ACCESS.allowed("no_such_tool", {"ADM"}) is False

    def test_user_without_roles_gets_nothing_but_wildcard(self) -> None:
        assert self.ACCESS.names_for(set()) == {"visualize"}

    def test_names_for_role(self) -> None:
        assert self.ACCESS.names_for({"DEV"}) == {"list_targets", "visualize"}


class TestRegistryFiltering:
    @staticmethod
    def _tools() -> list:
        @tool
        def query(sql: str) -> str:
            """только ADM"""
            return sql

        @tool
        def list_targets() -> str:
            """DEV и ADM"""
            return "ok"

        return [query, list_targets]

    def _registry(self) -> ToolRegistry:
        return ToolRegistry(
            tools=self._tools(),
            access=ToolAccess({"query": ["ADM"], "list_targets": ["DEV", "ADM"]}),
        )

    def test_admin_sees_everything(self) -> None:
        names = {t.name for t in self._registry().for_roles({"ADM"})}
        assert names == {"query", "list_targets"}

    def test_dev_does_not_see_query(self) -> None:
        names = {t.name for t in self._registry().for_roles({"DEV"})}
        assert names == {"list_targets"}

    def test_no_roles_no_tools(self) -> None:
        assert self._registry().for_roles(set()) == []


class TestAccessGuard:
    @staticmethod
    def _guarded(roles: set[str]):
        @tool
        def query(sql: str) -> str:
            """только ADM"""
            return f"executed: {sql}"

        access = ToolAccess({"query": ["ADM"]})
        return ToolAccessGuard.guard_all([query], access, lambda: roles)[0]

    def test_allowed_role_runs(self) -> None:
        assert self._guarded({"ADM"}).invoke({"sql": "select 1"}) == (
            "executed: select 1"
        )

    def test_denied_role_raises(self) -> None:
        with pytest.raises(ToolAccessDeniedError, match="query"):
            self._guarded({"DEV"}).invoke({"sql": "select 1"})

    def test_denied_is_ordinary_exception(self) -> None:
        assert issubclass(ToolAccessDeniedError, Exception)


class TestHistoryHidesForeignTools:
    """LLM не должна узнать о недоступном инструменте даже из истории."""

    @staticmethod
    def _history() -> list:
        return [
            HumanMessage(content="дай данные", id="u1"),
            AIMessage(
                content="",
                id="a1",
                tool_calls=[{"name": "query", "args": {}, "id": "c1",
                             "type": "tool_call"}],
            ),
            ToolMessage(content="rows", tool_call_id="c1", id="t1"),
        ]

    def test_foreign_call_removed_from_current_turn(self) -> None:
        view = build_llm_view(self._history(), frozenset({"list_targets"}))
        assert [type(m).__name__ for m in view] == ["HumanMessage"]

    def test_allowed_call_kept(self) -> None:
        view = build_llm_view(self._history(), frozenset({"query"}))
        assert len(view) == 3

    def test_no_filter_keeps_everything(self) -> None:
        assert len(build_llm_view(self._history(), None)) == 3

    def test_old_turns_never_carry_tool_calls(self) -> None:
        history = [*self._history(), HumanMessage(content="ещё", id="u2")]
        view = build_llm_view(history, frozenset({"query"}))
        assert not any(isinstance(m, ToolMessage) for m in view)
        assert not any(
            isinstance(m, AIMessage) and m.tool_calls for m in view
        )
