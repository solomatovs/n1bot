"""Хук PreToolUse без сети: матчер по форме хуков, полное имя инструмента,
чтение ответа обработчика, проверки настроек обработчика."""

from __future__ import annotations

import json

import pytest

from boba.agent.hooks import (
    HookDecision,
    HookFailure,
    HookMatcher,
    HookReplies,
    HookReply,
    HookReplyError,
    HttpHookSpec,
    McpToolOrigin,
    ToolOrigins,
)

URL = "http://127.0.0.1:1/hooks/pre-tool-use"


class TestMatcher:
    @pytest.mark.parametrize(
        ("pattern", "name", "expected"),
        [
            ("", "anything", True),
            ("*", "mcp__a__b", True),
            ("Bash", "Bash", True),
            ("Bash", "Bashful", False),
            ("Edit|Write", "Write", True),
            ("Edit, Write", "Edit", True),
            ("Edit, Write", "Read", False),
            ("mcp__memory__.*", "mcp__memory__create_entities", True),
            ("mcp__memory__.*", "mcp__files__read", False),
            ("mcp__memory", "mcp__memory__create_entities", False),
            ("^Notebook", "NotebookEdit", True),
            ("Edit.*", "NotebookEdit", True),
            ("^Edit$", "NotebookEdit", False),
            ("mcp__.*__write.*", "mcp__files__write_file", True),
        ],
    )
    def test_matches_by_the_documented_rules(
        self, pattern: str, name: str, expected: bool
    ) -> None:
        assert HookMatcher(pattern).matches(name) is expected

    def test_broken_regular_expression_is_refused(self) -> None:
        with pytest.raises(ValueError, match="not a valid regular expression"):
            HookMatcher("(")


class TestToolNames:
    def test_server_tool_gets_the_mcp_form_and_own_tool_keeps_its_name(self) -> None:
        origins = ToolOrigins(
            {
                "pg_query": McpToolOrigin(
                    server="boba_base", tool="pg_query", features={}
                )
            }
        )

        assert origins.full_name("pg_query") == "mcp__boba_base__pg_query"
        assert origins.full_name("AskUserQuestion") == "AskUserQuestion"
        assert origins.of("AskUserQuestion") is None

    def test_prefixed_tool_is_named_by_its_remote_name(self) -> None:
        origins = ToolOrigins(
            {"a_add": McpToolOrigin(server="a", tool="add", features={})}
        )

        assert origins.full_name("a_add") == "mcp__a__add"


class TestReplies:
    def test_empty_body_is_a_silent_allow(self) -> None:
        reading = HookReplies().read(HookReply(status=200, body=" \n"))

        assert reading.decision is None
        assert reading.updated_input is None
        assert reading.reason == ""

    def test_object_without_hook_output_gives_no_decision(self) -> None:
        reading = HookReplies().read(
            HookReply(status=200, body=json.dumps({"continue": True}))
        )

        assert reading.decision is None

    def test_allow_with_updated_input(self) -> None:
        body = {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "allow",
                "permissionDecisionReason": "sealed",
                "updatedInput": {"connection": "eyJ...", "statement": "select 1"},
                "additionalContext": "ignored",
            },
            "systemMessage": "ignored",
        }
        reading = HookReplies().read(HookReply(status=204, body=json.dumps(body)))

        assert reading.decision is HookDecision.ALLOW
        assert reading.reason == "sealed"
        assert reading.updated_input == {
            "connection": "eyJ...",
            "statement": "select 1",
        }

    def test_deny_with_reason(self) -> None:
        body = {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": "no grant",
            }
        }
        reading = HookReplies().read(HookReply(status=200, body=json.dumps(body)))

        assert reading.decision is HookDecision.DENY
        assert reading.reason == "no grant"
        assert reading.updated_input is None

    @pytest.mark.parametrize(
        ("reply", "complaint"),
        [
            (HookReply(status=500, body="boom"), "got 500"),
            (HookReply(status=200, body="not json"), "expected a JSON object"),
            (HookReply(status=200, body="[1, 2]"), "expected a JSON object"),
            (
                HookReply(
                    status=200,
                    body=json.dumps(
                        {"hookSpecificOutput": {"permissionDecision": "defer"}}
                    ),
                ),
                "permissionDecision",
            ),
        ],
    )
    def test_unusable_reply_is_an_error(self, reply: HookReply, complaint: str) -> None:
        with pytest.raises(HookReplyError, match=complaint):
            HookReplies().read(reply)


class TestSpec:
    def test_timeout_must_be_positive(self) -> None:
        with pytest.raises(ValueError, match="timeout must be positive"):
            HttpHookSpec(
                url=URL, headers={}, timeout_sec=0, on_failure=HookFailure.BLOCK
            )
