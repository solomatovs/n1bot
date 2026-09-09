"""VisualResult: сводка для LLM и восстановление из сериализованного dict."""

from __future__ import annotations

from boba.toolkit.result import ShellResult, ToolArtifact, VisualResult


class TestRenderForLlm:
    def test_with_title(self) -> None:
        result = VisualResult(
            element="Mermaid", props={"spec": "erDiagram"}, title="Схема заказов"
        )
        if result.llm_view() != "[Mermaid rendered: Схема заказов]":
            raise AssertionError('result.llm_view() == "[Mermaid rendered: Схема…')

    def test_without_title(self) -> None:
        result = VisualResult(element="Mermaid", props={"spec": "erDiagram"})
        if result.llm_view() != "[Mermaid rendered]":
            raise AssertionError('result.llm_view() == "[Mermaid rendered]"')


class TestRevive:
    """langgraph сериализует artifact в dict — revive обязан поднять модель."""

    def test_round_trip(self) -> None:
        result = VisualResult(
            element="Mermaid",
            props={"spec": "erDiagram", "type": "erDiagram", "title": None},
            title=None,
        )

        revived = ToolArtifact.revive(result.model_dump())

        if not (isinstance(revived, VisualResult)):
            raise AssertionError("isinstance(revived, VisualResult)")
        if revived != result:
            raise AssertionError("revived == result")

    def test_unknown_kind_is_ignored(self) -> None:
        if ToolArtifact.revive({"kind": "no_such_kind"}) is not None:
            raise AssertionError('ToolArtifact.revive({"kind": "no_such_kind"}) is No…')

    def test_fields_of_a_past_version_are_dropped(self) -> None:
        """История хранит артефакты прежних версий: лишнее поле не роняет тред."""
        stored = {
            "kind": "shell",
            "ok": True,
            "exit_code": 0,
            "stdout": "hi",
            "stdout_bytes": 2,
            "stdout_truncated": False,
            "stderr": "",
            "stderr_bytes": 0,
            "stderr_truncated": False,
            "duration_ms": 1,
            "timed_out": False,
            "diagnostic": "",
        }

        revived = ToolArtifact.revive(stored)

        if not isinstance(revived, ShellResult):
            raise AssertionError(revived)
        if revived.stdout != "hi":
            raise AssertionError(revived.stdout)
