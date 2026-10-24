"""Распознавание переполнения окна по тексту ошибки сервера и превышение,
которое сервер называет числами (OpenAI, vLLM, Anthropic)."""

from __future__ import annotations

import pytest

from boba.llm.chat import ContextOverflowSigns


class TestExcess:
    @pytest.mark.parametrize(
        ("text", "excess"),
        [
            (
                "This model's maximum context length is 4096 tokens. However, your "
                "messages resulted in 5000 tokens. Please reduce the length.",
                904,
            ),
            (
                "This model's maximum context length is 32768 tokens. However, you "
                "requested 33000 tokens (29000 in the messages, 4000 in the "
                "completion).",
                232,
            ),
            ("prompt is too long: 210000 tokens > 200000 maximum", 10_000),
        ],
        ids=["openai", "vllm", "anthropic"],
    )
    def test_numbers_named_by_the_server(self, text: str, excess: int) -> None:
        signs = ContextOverflowSigns()

        assert signs.matches(text)
        assert signs.excess(text) == excess

    def test_overflow_without_numbers_has_no_excess(self) -> None:
        signs = ContextOverflowSigns()
        text = "the input is too long for this model"

        assert signs.matches(text)
        assert signs.excess(text) is None
