"""Доступ к инструментам в приложении: фильтр реестра и гвардия на вызове."""

from __future__ import annotations

import asyncio

import pytest
from pydantic import BaseModel

from boba.access import ProfileGrant, RoleConfig, ToolAccess
from boba.runtime.plugins import PluginMeta
from boba.stand.refs import StandRefs
from boba.stand.toolstand import ProbeTools
from boba.toolkit.chain import CallAmbient
from boba.toolkit.dag import NodeOutcome
from boba.toolkit.ports import StreamSpecs
from boba.toolkit.result import MarkdownResult, ToolResultBase
from boba.toolrun.access import ToolAccessDeniedError, ToolAccessGuard
from boba.toolrun.hosted import HostedTool
from boba.toolrun.registry import ToolRegistry


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


class TestRegistryFiltering:
    @staticmethod
    def _tools() -> list[HostedTool]:
        probes = ProbeTools()
        tools: list[HostedTool] = []
        for name in ("query", "list_targets"):
            tools.append(probes.recorder(name, _QueryArgs))

        return tools

    def _registry(self) -> ToolRegistry:
        access = ToolAccess(
            tool_names=["query", "list_targets"],
            roles={
                "ADM": RoleConfig(tools=["*"]),
                "DEV": RoleConfig(tools=["list_targets"]),
            },
            profiles={"general": ProfileGrant(tools=["*"], roles=["*"])},
        )
        tools = self._tools()
        return ToolRegistry(
            tools=tools,
            access=access,
            stream_config=StandRefs.STREAM_CONFIG,
            own=frozenset(),
            node_args=(),
            specs=StreamSpecs({}),
            ambient=CallAmbient(),
        )

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
        if self._registry().for_session({"ADM"}, "") != []:
            raise AssertionError('for_session({"ADM"}, "") == []')


class _AccessFacts:
    """Факты о вызывающем в объёме гвардии доступа: роли и профиль."""

    def __init__(self, roles: set[str], profile: str | None) -> None:
        self.roles = frozenset(roles)
        self.profile = ""
        if profile is not None:
            self.profile = profile


class _QueryArgs(BaseModel):
    """Аргументы пробного инструмента."""

    sql: str


class TestAccessGuard:
    @staticmethod
    def _guarded(roles: set[str], profile: str | None) -> HostedTool:
        async def body(sql: str) -> ToolResultBase:
            return MarkdownResult(text=f"executed: {sql}")

        query = ProbeTools().hosted("query", _QueryArgs, body)

        access = ToolAccess(
            tool_names=["query"],
            roles={"ADM": RoleConfig(tools=["query"])},
            profiles={"general": ProfileGrant(tools=["*"], roles=["*"])},
        )
        facts = _AccessFacts(roles, profile)
        guarded = ToolAccessGuard(lambda: facts).guard_all([query], access)
        return guarded[0]

    @staticmethod
    def _called(tool: HostedTool) -> NodeOutcome:
        return asyncio.run(ProbeTools().call(tool, "c1", {"sql": "select 1"}))

    def test_allowed_role_runs(self) -> None:
        result = self._called(self._guarded({"ADM"}, "general")).content
        if result != "executed: select 1":
            raise AssertionError('result == "executed: select 1"')

    def test_denied_role_raises(self) -> None:
        with pytest.raises(ToolAccessDeniedError, match="query"):
            self._called(self._guarded({"DEV"}, "general"))

    def test_missing_profile_raises(self) -> None:
        with pytest.raises(ToolAccessDeniedError, match="query"):
            self._called(self._guarded({"ADM"}, None))

    def test_denied_is_ordinary_exception(self) -> None:
        if not (issubclass(ToolAccessDeniedError, Exception)):
            raise AssertionError("issubclass(ToolAccessDeniedError, Exception)")
