"""Тесты профилей чата: реестр, выбор профиля сессии, параметры LLM."""

from __future__ import annotations

import pytest

from boba.chat.profiles import (
    AgentSettings,
    ChatProfileConfig,
    ChatProfiles,
    ProfileRefusal,
)
from boba.chat.provider import OpenAiChatConfig
from boba.identity.errors import RefusalError

OPENAI = {"base_url": "https://llm.example/v1", "api_key": "token"}

BACKEND = {"provider": "openai", "openai": OPENAI}


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    pass


def _profile(**kw) -> ChatProfileConfig:
    base = {
        "display_name": "Profile",
        "description": "test profile",
        "backend": BACKEND,
        "model": "test-model",
    }
    base.update(kw)
    return ChatProfileConfig.model_validate(base)


class TestRegistryValidation:
    def test_no_profiles_is_config_error(self) -> None:
        with pytest.raises(ValueError, match="at least one"):
            ChatProfiles({})

    def test_no_default_is_config_error(self) -> None:
        profiles = {"a": _profile(), "b": _profile()}
        with pytest.raises(ValueError, match="default"):
            ChatProfiles(profiles)

    def test_two_defaults_is_config_error(self) -> None:
        profiles = {"a": _profile(default=True), "b": _profile(default=True)}
        with pytest.raises(ValueError, match="default"):
            ChatProfiles(profiles)


class TestVisibility:
    REGISTRY = ChatProfiles(
        {
            "general": _profile(default=True, roles=["*"]),
            "admin": _profile(roles=["ADM"]),
        }
    )

    def test_wildcard_profile_visible_to_any_role(self) -> None:
        visible = self.REGISTRY.visible_for(frozenset({"DEV"}))
        if set(visible) != {"general"}:
            raise AssertionError('set(visible) == {"general"}')

    def test_role_bound_profile_visible_to_its_role(self) -> None:
        visible = self.REGISTRY.visible_for(frozenset({"ADM"}))
        if set(visible) != {"general", "admin"}:
            raise AssertionError('set(visible) == {"general", "admin"}')

    def test_wildcard_needs_at_least_one_role(self) -> None:
        if self.REGISTRY.visible_for(frozenset()) != {}:
            raise AssertionError("visible_for(frozenset()) == {}")


class TestResolve:
    REGISTRY = ChatProfiles(
        {
            "general": _profile(default=True, roles=["*"]),
            "admin": _profile(roles=["ADM"]),
        }
    )

    def test_selected_profile_resolves(self) -> None:
        selected = self.REGISTRY.resolve("admin", frozenset({"ADM"}))
        if selected.name != "admin":
            raise AssertionError('selected.name == "admin"')

    def test_foreign_profile_is_refused(self) -> None:
        with pytest.raises(RefusalError, match="not available"):
            self.REGISTRY.resolve("admin", frozenset({"DEV"}))

    def test_unselected_with_single_visible_is_auto_assigned(self) -> None:
        selected = self.REGISTRY.resolve(None, frozenset({"DEV"}))
        if selected.name != "general":
            raise AssertionError('selected.name == "general"')

    def test_unselected_with_many_visible_is_refused(self) -> None:
        with pytest.raises(RefusalError, match="select a chat profile"):
            self.REGISTRY.resolve(None, frozenset({"ADM"}))

    def test_no_roles_no_profiles_is_refused(self) -> None:
        with pytest.raises(RefusalError) as info:
            self.REGISTRY.resolve(None, frozenset())

        if info.value.kind != ProfileRefusal.NO_PROFILE_ACCESS:
            raise AssertionError("info.value.kind == NO_PROFILE_ACCESS")


class TestVisibilityByWildcard:
    def test_wildcard_role_covers_profile_roles(self) -> None:
        profile = _profile(roles=["*"])
        if profile.visible_for(frozenset({"ANY"})) is not True:
            raise AssertionError('visible_for({"ANY"}) is True')

    def test_named_roles_intersect(self) -> None:
        profile = _profile(roles=["ADM", "DEV"])
        if profile.visible_for(frozenset({"DEV"})) is not True:
            raise AssertionError('visible_for({"DEV"}) is True')
        if profile.visible_for(frozenset({"OTHER"})) is not False:
            raise AssertionError('visible_for({"OTHER"}) is False')


class TestChatSampling:
    def test_empty_sampling_sends_nothing(self) -> None:
        settings = AgentSettings.model_validate(
            {"backend": BACKEND, "model": "test-model"}
        )
        if settings.chat_sampling() != {}:
            raise AssertionError("settings.chat_sampling() == {}")

    def test_admin_params_pass_through_verbatim(self) -> None:
        """Таблица sampling уходит как написана: без проверок и переименований."""
        settings = AgentSettings.model_validate(
            {
                "backend": BACKEND,
                "model": "test-model",
                "sampling": {
                    "temperature": 0.2,
                    "max_completion_tokens": 1000,
                    "top_k": 40,
                    "repetition_penalty": 1.1,
                    "stop": ["END"],
                },
            }
        )
        sampling = settings.chat_sampling()
        expected = {
            "temperature": 0.2,
            "max_completion_tokens": 1000,
            "top_k": 40,
            "repetition_penalty": 1.1,
            "stop": ["END"],
        }
        if sampling != expected:
            raise AssertionError(f"sampling: {sampling}")

    def test_openai_transport_binds(self) -> None:
        settings = AgentSettings.model_validate(
            {"backend": BACKEND, "model": "test-model"}
        )
        if not isinstance(settings.backend, OpenAiChatConfig):
            raise AssertionError("isinstance(settings.backend, OpenAiChatConfig)")
