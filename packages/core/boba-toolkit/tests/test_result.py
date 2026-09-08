"""VisualResult: сводка для LLM и восстановление из сериализованного dict."""

from __future__ import annotations

from boba.toolkit.result import ToolArtifact, VisualResult


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
