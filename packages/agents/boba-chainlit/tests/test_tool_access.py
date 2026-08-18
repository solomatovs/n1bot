"""Тесты доступа к инструментам: пересечение ролей и профиля, отказ на вызове."""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool

from boba.chainlit.agent.toolrun.access import (
    ToolAccess,
    ToolAccessDeniedError,
    ToolAccessGuard,
)
from boba.chainlit.domain.config import RoleConfig, ToolGrant
from boba.chainlit.infra.plugins import PluginMeta, ToolRegistry
from boba.chainlit.infra.providers import build_llm_view


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    pass


class TestPluginMetaNotation:
    def test_tools_list_is_the_allowlist(self) -> None:
        meta = PluginMeta(enable=True, tools=["a", "b"])
        if "a" not in meta.tools:
            raise AssertionError('"a" in meta.tools')
        if "c" in meta.tools:
            raise AssertionError('"c" not in meta.tools')

    def test_tools_default_is_empty(self) -> None:
        meta = PluginMeta(enable=True)
        if meta.tools != []:
            raise AssertionError("meta.tools == []")


class TestToolGrant:
    def test_named_tool_covered(self) -> None:
        grant = ToolGrant(tools=["query"])
        if grant.covers("query") is not True:
            raise AssertionError('grant.covers("query") is True')

    def test_other_tool_not_covered(self) -> None:
        grant = ToolGrant(tools=["query"])
        if grant.covers("list_targets") is not False:
            raise AssertionError('grant.covers("list_targets") is False')

    def test_wildcard_covers_everything(self) -> None:
        grant = ToolGrant(tools=["*"])
        if grant.covers("whatever") is not True:
            raise AssertionError('grant.covers("whatever") is True')

    def test_empty_grant_covers_nothing(self) -> None:
        grant = ToolGrant(tools=[])
        if grant.covers("query") is not False:
            raise AssertionError('grant.covers("query") is False')

    def test_unknown_reports_typos(self) -> None:
        grant = ToolGrant(tools=["query", "no_such", "*"])
        if grant.unknown(frozenset({"query"})) != ["no_such"]:
            raise AssertionError('grant.unknown(...) == ["no_such"]')


class TestToolAccess:
    ACCESS = ToolAccess(
        tool_names=["query", "list_targets", "visualize"],
        roles={
            "ADM": RoleConfig(tools=["*"]),
            "DEV": RoleConfig(tools=["list_targets", "visualize"]),
            "EMPTY": RoleConfig(tools=[]),
        },
        profiles={
            "general": ToolGrant(tools=["*"]),
            "search": ToolGrant(tools=["list_targets"]),
        },
    )

    def test_role_and_profile_both_cover(self) -> None:
        if self.ACCESS.allowed("query", {"ADM"}, "general") is not True:
            raise AssertionError('allowed("query", {"ADM"}, "general") is True')

    def test_profile_cuts_role_wildcard(self) -> None:
        if self.ACCESS.allowed("query", {"ADM"}, "search") is not False:
            raise AssertionError('allowed("query", {"ADM"}, "search") is False')

    def test_role_cuts_profile_wildcard(self) -> None:
        if self.ACCESS.allowed("query", {"DEV"}, "general") is not False:
            raise AssertionError('allowed("query", {"DEV"}, "general") is False')

    def test_any_role_intersection_is_enough(self) -> None:
        if self.ACCESS.allowed("query", {"DEV", "ADM"}, "general") is not True:
            raise AssertionError('allowed("query", {"DEV","ADM"}, "general") is True')

    def test_no_profile_denies(self) -> None:
        if self.ACCESS.allowed("query", {"ADM"}, None) is not False:
            raise AssertionError('allowed("query", {"ADM"}, None) is False')

    def test_unknown_profile_denies(self) -> None:
        if self.ACCESS.allowed("query", {"ADM"}, "ghost") is not False:
            raise AssertionError('allowed("query", {"ADM"}, "ghost") is False')

    def test_unknown_role_denies(self) -> None:
        if self.ACCESS.allowed("query", {"GHOST"}, "general") is not False:
            raise AssertionError('allowed("query", {"GHOST"}, "general") is False')

    def test_no_roles_denies_even_wildcard_profile(self) -> None:
        if self.ACCESS.allowed("query", set(), "general") is not False:
            raise AssertionError('allowed("query", set(), "general") is False')

    def test_empty_role_grant_denies(self) -> None:
        if self.ACCESS.allowed("query", {"EMPTY"}, "general") is not False:
            raise AssertionError('allowed("query", {"EMPTY"}, "general") is False')

    def test_unknown_tool_denied(self) -> None:
        if self.ACCESS.allowed("no_such_tool", {"ADM"}, "general") is not False:
            raise AssertionError('allowed("no_such_tool", ...) is False')

    def test_names_for_intersection(self) -> None:
        names = self.ACCESS.names_for({"DEV"}, "general")
        if names != {"list_targets", "visualize"}:
            raise AssertionError('names == {"list_targets", "visualize"}')

    def test_names_for_narrow_profile(self) -> None:
        names = self.ACCESS.names_for({"ADM"}, "search")
        if names != {"list_targets"}:
            raise AssertionError('names == {"list_targets"}')

    def test_names_without_profile_empty(self) -> None:
        if self.ACCESS.names_for({"ADM"}, None) != set():
            raise AssertionError("names_for({'ADM'}, None) == set()")


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
        access = ToolAccess(
            tool_names=["query", "list_targets"],
            roles={
                "ADM": RoleConfig(tools=["*"]),
                "DEV": RoleConfig(tools=["list_targets"]),
            },
            profiles={"general": ToolGrant(tools=["*"])},
        )
        return ToolRegistry(tools=self._tools(), access=access)

    def test_admin_sees_everything(self) -> None:
        names = {t.name for t in self._registry().for_session({"ADM"}, "general")}
        if names != {"query", "list_targets"}:
            raise AssertionError('names == {"query", "list_targets"}')

    def test_dev_does_not_see_query(self) -> None:
        names = {t.name for t in self._registry().for_session({"DEV"}, "general")}
        if names != {"list_targets"}:
            raise AssertionError('names == {"list_targets"}')

    def test_no_roles_no_tools(self) -> None:
        if self._registry().for_session(set(), "general") != []:
            raise AssertionError('for_session(set(), "general") == []')

    def test_no_profile_no_tools(self) -> None:
        if self._registry().for_session({"ADM"}, None) != []:
            raise AssertionError('for_session({"ADM"}, None) == []')


class TestAccessGuard:
    @staticmethod
    def _guarded(roles: set[str], profile: str | None):
        @tool
        def query(sql: str) -> str:
            """только ADM"""
            return f"executed: {sql}"

        access = ToolAccess(
            tool_names=["query"],
            roles={"ADM": RoleConfig(tools=["query"])},
            profiles={"general": ToolGrant(tools=["*"])},
        )
        guarded = ToolAccessGuard.guard_all(
            [query], access, lambda: roles, lambda: profile
        )
        return guarded[0]

    def test_allowed_role_runs(self) -> None:
        result = self._guarded({"ADM"}, "general").invoke({"sql": "select 1"})
        if result != "executed: select 1":
            raise AssertionError('result == "executed: select 1"')

    def test_denied_role_raises(self) -> None:
        with pytest.raises(ToolAccessDeniedError, match="query"):
            self._guarded({"DEV"}, "general").invoke({"sql": "select 1"})

    def test_missing_profile_raises(self) -> None:
        with pytest.raises(ToolAccessDeniedError, match="query"):
            self._guarded({"ADM"}, None).invoke({"sql": "select 1"})

    def test_denied_is_ordinary_exception(self) -> None:
        if not (issubclass(ToolAccessDeniedError, Exception)):
            raise AssertionError("issubclass(ToolAccessDeniedError, Exception)")


class TestHistoryHidesForeignTools:
    """LLM не должна узнать о недоступном инструменте даже из истории."""

    @staticmethod
    def _history() -> list:
        return [
            HumanMessage(content="дай данные", id="u1"),
            AIMessage(
                content="",
                id="a1",
                tool_calls=[
                    {"name": "query", "args": {}, "id": "c1", "type": "tool_call"}
                ],
            ),
            ToolMessage(content="rows", tool_call_id="c1", id="t1"),
        ]

    def test_foreign_call_removed_from_current_turn(self) -> None:
        view = build_llm_view(self._history(), frozenset({"list_targets"}))
        if [type(m).__name__ for m in view] != ["HumanMessage"]:
            raise AssertionError('[type(m).__name__ for m in view] == ["HumanMessage"]')

    def test_allowed_call_kept(self) -> None:
        view = build_llm_view(self._history(), frozenset({"query"}))
        if len(view) != 3:
            raise AssertionError("len(view) == 3")

    def test_no_filter_keeps_everything(self) -> None:
        if len(build_llm_view(self._history(), None)) != 3:
            raise AssertionError("len(build_llm_view(self._history(), None)) == 3")

    @staticmethod
    def _long_history(turns: int) -> list:
        messages: list = []
        for i in range(turns):
            messages.append(HumanMessage(content=f"вопрос {i}", id=f"u{i}"))
            messages.append(AIMessage(content=f"ответ {i}", id=f"a{i}"))
        messages.append(HumanMessage(content="текущий", id="now"))
        return messages

    def test_history_window_limits_old_messages(self) -> None:
        view = build_llm_view(self._long_history(20), None, history_messages=5)
        # 5 старых реплик + текущий ход
        if len(view) != 6:
            raise AssertionError("len(view) == 6")
        if view[-1].content != "текущий":
            raise AssertionError('view[-1].content == "текущий"')

    def test_history_window_keeps_the_newest(self) -> None:
        view = build_llm_view(self._long_history(20), None, history_messages=2)
        if [m.content for m in view[:-1]] != ["вопрос 19", "ответ 19"]:
            raise AssertionError('[m.content for m in view[:-1]] == ["вопрос 19", "от…')

    def test_history_window_default_matches_config(self) -> None:
        from boba.chainlit.infra.config import AgentSettings

        default = AgentSettings.model_fields["history_messages"].default
        view = build_llm_view(self._long_history(100), None)
        if len(view) != default + 1:
            raise AssertionError("len(view) == default + 1")

    def test_short_history_is_not_padded(self) -> None:
        view = build_llm_view(self._long_history(2), None, history_messages=50)
        if len(view) != 5:
            raise AssertionError("len(view) == 5")

    def test_old_turns_never_carry_tool_calls(self) -> None:
        history = [*self._history(), HumanMessage(content="ещё", id="u2")]
        view = build_llm_view(history, frozenset({"query"}))
        if any(isinstance(m, ToolMessage) for m in view):
            raise AssertionError("not any(isinstance(m, ToolMessage) for m in view)")
        if any(isinstance(m, AIMessage) and m.tool_calls for m in view):
            raise AssertionError("not any(isinstance(m, AIMessage) and m.tool_calls f…")
