"""Решение о доступе: пересечение ролей и профиля, видимость профиля, chat_only."""

from __future__ import annotations

import pytest

from boba.access import (
    ProfileGrant,
    ToolAccess,
    ToolAccessError,
    ToolAvailability,
    ToolGrant,
)


class TestToolGrant:
    def test_named_tool_covered(self) -> None:
        assert ToolGrant(tools=["query"]).covers("query") is True

    def test_other_tool_not_covered(self) -> None:
        assert ToolGrant(tools=["query"]).covers("list_targets") is False

    def test_wildcard_covers_everything(self) -> None:
        assert ToolGrant(tools=["*"]).covers("whatever") is True

    def test_empty_grant_covers_nothing(self) -> None:
        assert ToolGrant(tools=[]).covers("query") is False

    def test_unknown_reports_typos(self) -> None:
        grant = ToolGrant(tools=["query", "no_such", "*"])
        assert grant.unknown(frozenset({"query"})) == ["no_such"]


class TestProfileGrant:
    def test_wildcard_roles_visible_to_anyone_with_a_role(self) -> None:
        assert ProfileGrant(roles=["*"]).visible_for({"X"}) is True

    def test_no_roles_never_visible(self) -> None:
        assert ProfileGrant(roles=["*"]).visible_for(set()) is False

    def test_intersection(self) -> None:
        grant = ProfileGrant(roles=["A", "B"])
        assert grant.visible_for({"B", "C"}) is True
        assert grant.visible_for({"C"}) is False


class TestToolAccess:
    ACCESS = ToolAccess(
        tool_names=["query", "list_targets", "visualize", "canvas_open"],
        roles={
            "ADM": ToolGrant(tools=["*"]),
            "DEV": ToolGrant(tools=["list_targets", "visualize", "canvas_open"]),
            "EMPTY": ToolGrant(tools=[]),
        },
        profiles={
            "general": ProfileGrant(tools=["*"], roles=["*"]),
            "search": ProfileGrant(tools=["list_targets"], roles=["*"]),
            "adm-only": ProfileGrant(tools=["*"], roles=["ADM"]),
        },
        chat_only=["canvas_open"],
    )

    def test_role_and_profile_both_cover(self) -> None:
        assert self.ACCESS.decide("query", {"ADM"}, "general").headless

    def test_profile_cuts_role_wildcard(self) -> None:
        assert self.ACCESS.allowed("query", {"ADM"}, "search") is False

    def test_role_cuts_profile_wildcard(self) -> None:
        assert self.ACCESS.allowed("query", {"DEV"}, "general") is False

    def test_any_role_intersection_is_enough(self) -> None:
        assert self.ACCESS.allowed("query", {"DEV", "ADM"}, "general") is True

    def test_profile_invisible_to_roles_denies(self) -> None:
        assert self.ACCESS.allowed("query", {"DEV"}, "adm-only") is False
        assert self.ACCESS.allowed("query", {"ADM"}, "adm-only") is True

    def test_unknown_profile_denies(self) -> None:
        assert self.ACCESS.allowed("query", {"ADM"}, "ghost") is False

    def test_empty_profile_denies(self) -> None:
        assert self.ACCESS.allowed("query", {"ADM"}, "") is False

    def test_unknown_role_denies(self) -> None:
        assert self.ACCESS.allowed("query", {"GHOST"}, "general") is False

    def test_no_roles_denies_even_wildcard_profile(self) -> None:
        assert self.ACCESS.allowed("query", set(), "general") is False

    def test_empty_role_grant_denies(self) -> None:
        assert self.ACCESS.allowed("query", {"EMPTY"}, "general") is False

    def test_unknown_tool_denied(self) -> None:
        assert self.ACCESS.decide("nope", {"ADM"}, "general") is ToolAvailability.DENIED

    def test_chat_only_is_allowed_in_chat_but_not_headless(self) -> None:
        decision = self.ACCESS.decide("canvas_open", {"DEV"}, "general")
        assert decision is ToolAvailability.CHAT_ONLY
        assert decision.in_chat
        assert not decision.headless

    def test_chat_only_still_needs_grants(self) -> None:
        decision = self.ACCESS.decide("canvas_open", {"EMPTY"}, "general")
        assert decision is ToolAvailability.DENIED


class TestGrantChecks:
    def test_role_typo_is_refused(self) -> None:
        with pytest.raises(ToolAccessError, match=r"'ADM'.*no_such"):
            ToolAccess(["query"], {"ADM": ToolGrant(tools=["no_such"])}, {})

    def test_profile_typo_is_refused(self) -> None:
        with pytest.raises(ToolAccessError, match=r"'p'.*no_such"):
            ToolAccess(["query"], {}, {"p": ProfileGrant(tools=["no_such"])})

    def test_stray_chat_only_is_refused(self) -> None:
        with pytest.raises(ToolAccessError, match="ghost"):
            ToolAccess(["query"], {}, {}, chat_only=["ghost"])
