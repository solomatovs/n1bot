"""FieldTemplate: поля, подстановка, обратный разбор, отказы."""

from __future__ import annotations

import pytest

from boba.toolkit.template import FieldTemplate, TemplateError


class TestFieldTemplate:
    def test_fields_in_order_without_repeats(self) -> None:
        template = FieldTemplate.parse("/{user_id}/{thread_id}/{user_id}")

        assert template.fields() == ("user_id", "thread_id")

    def test_render_and_missing_value(self) -> None:
        template = FieldTemplate.parse("{username}@REALM")

        assert template.render({"username": "bob"}) == "bob@REALM"

        with pytest.raises(TemplateError):
            template.render({})

    def test_extract_around_single_field(self) -> None:
        assert (
            FieldTemplate.parse("{username}@REALM").extract("bob@REALM", "username")
            == "bob"
        )
        assert (
            FieldTemplate.parse("DOMAIN\\{username}").extract("DOMAIN\\bob", "username")
            == "bob"
        )
        assert FieldTemplate.parse("{username}").extract("bob", "username") == "bob"

    def test_escaped_braces_are_literal(self) -> None:
        template = FieldTemplate.parse("{{{username}}}")

        assert template.render({"username": "bob"}) == "{bob}"
        assert template.extract("{bob}", "username") == "bob"

    @pytest.mark.parametrize(
        ("template", "text"),
        [
            ("{username}@REALM", "bob@OTHER"),
            ("{username}@REALM", "@REALM"),
            ("DOMAIN\\{username}", "bob"),
        ],
    )
    def test_extract_rejects_foreign_text(self, template: str, text: str) -> None:
        with pytest.raises(TemplateError):
            FieldTemplate.parse(template).extract(text, "username")

    def test_extract_needs_exactly_one_field(self) -> None:
        with pytest.raises(TemplateError):
            FieldTemplate.parse("{a}/{b}").extract("x/y", "a")

        with pytest.raises(TemplateError):
            FieldTemplate.parse("{a}/{a}").extract("x/x", "a")

        with pytest.raises(TemplateError):
            FieldTemplate.parse("plain").extract("plain", "a")

    def test_only_and_having(self) -> None:
        template = FieldTemplate.parse("/{user_id}/{thread_id}")

        assert template.only(("user_id", "thread_id")) is template
        assert template.having("user_id") is template

        with pytest.raises(TemplateError):
            template.only(("user_id",))

        with pytest.raises(TemplateError):
            template.having("nope")

    @pytest.mark.parametrize("text", ["{", "{}", "{0}", "{name:>4}", "{name!r}"])
    def test_bad_syntax(self, text: str) -> None:
        with pytest.raises(TemplateError):
            FieldTemplate.parse(text)
