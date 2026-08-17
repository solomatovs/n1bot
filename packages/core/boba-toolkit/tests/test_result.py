"""CustomElementResult: сводка для LLM и восстановление из сериализованного dict."""

from __future__ import annotations

from boba.toolkit.result import CustomElementResult, ToolArtifact, render_for_llm


class TestRenderForLlm:
    def test_with_title(self) -> None:
        result = CustomElementResult(
            element="Mermaid", props={"spec": "erDiagram"}, title="Схема заказов"
        )
        if render_for_llm(result) != "[Mermaid rendered: Схема заказов]":
            raise AssertionError('render_for_llm(result) == "[Mermaid rendered: Схема…')

    def test_without_title(self) -> None:
        result = CustomElementResult(element="Mermaid", props={"spec": "erDiagram"})
        if render_for_llm(result) != "[Mermaid rendered]":
            raise AssertionError('render_for_llm(result) == "[Mermaid rendered]"')


class TestRevive:
    """langgraph сериализует artifact в dict — revive обязан поднять модель."""

    def test_round_trip(self) -> None:
        result = CustomElementResult(
            element="Mermaid",
            props={"spec": "erDiagram", "type": "erDiagram", "title": None},
            title=None,
        )

        revived = ToolArtifact.revive(result.model_dump())

        if not (isinstance(revived, CustomElementResult)):
            raise AssertionError("isinstance(revived, CustomElementResult)")
        if revived != result:
            raise AssertionError("revived == result")

    def test_unknown_kind_is_ignored(self) -> None:
        if ToolArtifact.revive({"kind": "no_such_kind"}) is not None:
            raise AssertionError('ToolArtifact.revive({"kind": "no_such_kind"}) is No…')
