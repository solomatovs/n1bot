"""Разрешения: таблица решений по шести режимам, свёртка записей режима
и правил, потолок режима из параметров запуска."""

from __future__ import annotations

import pytest

from boba.agent.permissions import (
    Allowed,
    Asked,
    Classified,
    Decision,
    Denied,
    ModeCeiling,
    PermissionModeError,
    PermissionPolicy,
    PermissionState,
)
from boba.agent.records import (
    PermissionBehavior,
    PermissionMode,
    PermissionModeRecord,
    PermissionRule,
    PermissionRuleOperation,
    PermissionRuleRecord,
    ServiceRecord,
)
from boba.agent.template import AgentTemplate
from boba.toolkit.dag import ToolHints

STAMP = "2026-10-10T10:00:00.000Z"
SESSION = "p-1"
AGENT = "Boba"

READ_ONLY = ToolHints(read_only=True, destructive=False)
MUTATING = ToolHints(read_only=False, destructive=False)
DESTRUCTIVE = ToolHints()


def mode(value: PermissionMode) -> PermissionModeRecord:
    return PermissionModeRecord(
        timestamp=STAMP, session_id=SESSION, permission_mode=value
    )


def rule(tool: str, operation: PermissionRuleOperation) -> PermissionRuleRecord:
    return PermissionRuleRecord(
        timestamp=STAMP,
        session_id=SESSION,
        operation=operation,
        rule=PermissionRule(tool=tool, behavior=PermissionBehavior.ALLOW),
    )


def policy(records: list[ServiceRecord]) -> PermissionPolicy:
    return PermissionPolicy(PermissionState(records), AgentTemplate(), AGENT)


def shape(decision: Decision) -> str:
    return type(decision).__name__


class TestDecisionTable:
    """Один и тот же вызов в каждом режиме: читающий, меняющий, разрушающий
    инструмент и меняющий с правилом «до конца сессии»."""

    TABLE = {
        PermissionMode.DEFAULT: ("Allowed", "Asked", "Asked", "Allowed"),
        PermissionMode.ACCEPT_EDITS: ("Allowed", "Allowed", "Asked", "Allowed"),
        PermissionMode.PLAN: ("Allowed", "Denied", "Denied", "Denied"),
        PermissionMode.DONT_ASK: ("Allowed", "Denied", "Denied", "Allowed"),
        PermissionMode.BYPASS_PERMISSIONS: ("Allowed", "Allowed", "Allowed", "Allowed"),
        PermissionMode.AUTO: ("Allowed", "Classified", "Classified", "Allowed"),
    }

    @pytest.mark.parametrize("value", list(PermissionMode))
    def test_every_mode_matches_the_table(self, value: PermissionMode) -> None:
        decide = policy(
            [mode(value), rule("granted", PermissionRuleOperation.ADD)]
        ).decide

        observed = (
            shape(decide("reader", READ_ONLY)),
            shape(decide("writer", MUTATING)),
            shape(decide("dropper", DESTRUCTIVE)),
            shape(decide("granted", MUTATING)),
        )

        assert observed == self.TABLE[value], value

    def test_denial_texts_name_the_tool(self) -> None:
        plan = policy([mode(PermissionMode.PLAN)]).decide("writer", MUTATING)
        quiet = policy([mode(PermissionMode.DONT_ASK)]).decide("writer", MUTATING)

        assert isinstance(plan, Denied)
        assert "plan mode" in plan.text
        assert "writer" in plan.text
        assert isinstance(quiet, Denied)
        assert quiet.text == (
            "Boba requested permissions to use writer, but you haven't granted it yet."
        )

    def test_decision_kinds_are_distinct(self) -> None:
        assert Allowed() == Allowed()
        assert Asked() != Classified()


class TestFolding:
    def test_last_mode_wins_and_rules_follow_their_last_operation(self) -> None:
        state = PermissionState(
            [
                mode(PermissionMode.PLAN),
                rule("a", PermissionRuleOperation.ADD),
                rule("b", PermissionRuleOperation.ADD),
                mode(PermissionMode.DEFAULT),
                rule("a", PermissionRuleOperation.REMOVE),
                rule("a", PermissionRuleOperation.ADD),
                rule("b", PermissionRuleOperation.REMOVE),
            ]
        )

        assert state.mode is PermissionMode.DEFAULT
        assert state.allows("a")
        assert not state.allows("b")
        assert [r.tool for r in state.rules()] == ["a"]

    def test_without_records_the_mode_is_default(self) -> None:
        assert PermissionState([]).mode is PermissionMode.DEFAULT


class TestCeiling:
    def test_bypass_is_lowered_without_the_right(self) -> None:
        ceiling = ModeCeiling(allow_bypass=False, has_classifier=False)

        assert ceiling.capped(PermissionMode.BYPASS_PERMISSIONS) is (
            PermissionMode.DEFAULT
        )
        with pytest.raises(PermissionModeError, match="allow_bypass"):
            ceiling.check(PermissionMode.BYPASS_PERMISSIONS)

    def test_bypass_stays_with_the_right(self) -> None:
        ceiling = ModeCeiling(allow_bypass=True, has_classifier=False)

        assert ceiling.capped(PermissionMode.BYPASS_PERMISSIONS) is (
            PermissionMode.BYPASS_PERMISSIONS
        )

    def test_auto_needs_a_classifier(self) -> None:
        ceiling = ModeCeiling(allow_bypass=False, has_classifier=False)

        with pytest.raises(PermissionModeError, match="classifier"):
            ceiling.capped(PermissionMode.AUTO)

        assert ModeCeiling(False, True).capped(PermissionMode.AUTO) is (
            PermissionMode.AUTO
        )
