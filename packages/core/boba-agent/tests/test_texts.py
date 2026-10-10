"""Тексты пакета читаются целиком и несут фразы Claude Code, на которые
опирается план (разделы 5.13, 5.16, 5.17)."""

from __future__ import annotations

import pytest

from boba.agent.template import AgentTemplate, AgentTemplateFile


class TestAgentTexts:
    @pytest.mark.parametrize("text", list(AgentTemplateFile))
    def test_every_text_is_readable(self, text: AgentTemplateFile) -> None:
        body = AgentTemplate().read(text)

        assert body.strip()

    def test_summarize_instruction_names_no_claude_tools(self) -> None:
        body = AgentTemplate().read(AgentTemplateFile.SUMMARIZE_INSTRUCTION)

        assert "Do NOT use any tool." in body
        assert "Read, Bash, Grep" not in body
        assert "<summary>" in body

    def test_summary_wrapper_has_no_journal_path(self) -> None:
        body = AgentTemplate().read(AgentTemplateFile.SUMMARY_WRAPPER)

        assert body.startswith("This session is being continued")
        assert "{summary}" in body
        assert "read the full transcript" not in body

    def test_compaction_messages_match_claude_code(self) -> None:
        texts = AgentTemplate()

        assert texts.read(AgentTemplateFile.OUTPUT_LIMIT_HIT).startswith(
            "Output token limit hit."
        )
        no_response = texts.read(AgentTemplateFile.NO_RESPONSE_REQUESTED)

        assert no_response == "No response requested."
        assert texts.read(AgentTemplateFile.INTERRUPTED_TOOL_RESULT).startswith(
            "[Tool call interrupted:"
        )

    def test_templates_render_with_their_placeholders(self) -> None:
        texts = AgentTemplate()

        reminder = texts.read(AgentTemplateFile.SYSTEM_REMINDER).format(content="x")
        date = texts.read(AgentTemplateFile.DATE).format(date="2026-10-10")
        model = texts.read(AgentTemplateFile.MODEL_NAMED).format(
            marketing_name="Qwen", model_id="qwen3"
        )

        assert reminder == "<system-reminder>\nx\n</system-reminder>"
        assert date == "Today's date is 2026-10-10."
        assert model.endswith("The exact model ID is qwen3.")
