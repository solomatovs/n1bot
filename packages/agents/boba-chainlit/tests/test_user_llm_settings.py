"""Тесты слоя пользовательских настроек LLM: границы, наложение, хранение.

Сэмплинг пользователю недоступен: его задаёт администратор таблицей sampling
профиля, и она уходит в провайдер как написана; модель тоже фиксирована
профилем. Пользователю остаются глубина истории и личная добавка к промпту.
"""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID, uuid4

import pytest
from chainlit.input_widget import InputWidget, Slider, Tab
from chainlit.user import User as ChainlitUser
from psycopg import sql
from psycopg.rows import dict_row

from boba.chainlit.chat.panel_text import PanelText, PanelTextError
from boba.chainlit.chat.settings import PanelTab, SettingsPanel
from boba.chainlit.data.data_layer import PostgresDataLayer
from boba.chainlit.infra.config import AppConfig
from boba.chainlit.infra.session import current_session
from boba.chat.profiles import (
    ChatProfileConfig,
    SettingsBounds,
    SettingsView,
    UserLlmOverrides,
    UserMeta,
    UserSetting,
)
from boba.db.postgres import AsyncPostgresPool

pytestmark = pytest.mark.anyio

from boba.chat.provider import OpenAiChatConfig

OPENAI = {"base_url": "https://llm.example/v1", "api_key": "token"}

BACKEND = {"kind": "openai", "http": OPENAI}

NO_OVERRIDES = ""
"""Пустой app_root: строки берутся из пакета, как в чистом развёртывании."""


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    """Заглушка сессионной фикстуры: тесты слоя не ходят в контекст chainlit."""


BOUNDS = SettingsBounds()


def _panel(profile: ChatProfileConfig, saved: UserLlmOverrides) -> SettingsPanel:
    view = SettingsView.of(BOUNDS, profile, saved)
    return SettingsPanel(view, PanelText(NO_OVERRIDES, PanelText.DEFAULT_LANGUAGE))


def _profile(**kw) -> ChatProfileConfig:
    base = {
        "display_name": "Profile",
        "description": "test profile",
        "provider": BACKEND,
        "model": "base-model",
        "settings": ["*"],
        "system_prompt": "You are the profile assistant",
        "history_messages": 25,
        "sampling": {"temperature": 0.1, "top_k": 40},
    }
    base.update(kw)
    return ChatProfileConfig.model_validate(base)


class TestClamped:
    def test_history_is_clamped_into_bounds(self) -> None:
        overrides = UserLlmOverrides(history_messages=900)

        bounded = overrides.clamped(BOUNDS, _profile())

        if bounded.history_messages != 100:
            raise AssertionError(f"history: {bounded.history_messages}")

    def test_none_fields_stay_none(self) -> None:
        bounded = UserLlmOverrides().clamped(BOUNDS, _profile())

        if bounded.stored() != {}:
            raise AssertionError(f"stored: {bounded.stored()}")

    def test_disallowed_setting_is_dropped(self) -> None:
        profile = _profile(settings=["history_messages"])
        overrides = UserLlmOverrides(history_messages=10, user_prompt="sneak")

        bounded = overrides.clamped(BOUNDS, profile)

        if bounded.stored() != {"history_messages": 10}:
            raise AssertionError(f"stored: {bounded.stored()}")

    def test_empty_settings_disallow_everything(self) -> None:
        profile = _profile(settings=[])
        overrides = UserLlmOverrides(history_messages=10, user_prompt="sneak")

        bounded = overrides.clamped(BOUNDS, profile)

        if bounded.stored() != {}:
            raise AssertionError(f"stored: {bounded.stored()}")


class TestApplyTo:
    def test_override_wins_over_profile(self) -> None:
        overrides = UserLlmOverrides(history_messages=5)

        settings = overrides.apply_to(_profile())

        if settings.history_messages != 5:
            raise AssertionError(f"history: {settings.history_messages}")

    def test_none_keeps_profile_value(self) -> None:
        settings = UserLlmOverrides().apply_to(_profile())

        if settings.history_messages != 25:
            raise AssertionError(f"history: {settings.history_messages}")
        if settings.model != "base-model":
            raise AssertionError(f"model: {settings.model}")

    def test_user_prompt_is_appended(self) -> None:
        overrides = UserLlmOverrides(user_prompt="Answer in Russian")

        settings = overrides.apply_to(_profile())

        expected = "You are the profile assistant\n\nAnswer in Russian"
        if settings.system_prompt != expected:
            raise AssertionError(f"system_prompt: {settings.system_prompt!r}")

    def test_transport_is_never_overridden(self) -> None:
        settings = UserLlmOverrides(history_messages=1).apply_to(_profile())

        backend = settings.provider
        if not isinstance(backend, OpenAiChatConfig):
            raise AssertionError(f"backend is openai: {backend}")
        if backend.http.base_url != OPENAI["base_url"]:
            raise AssertionError("openai transport changed")

    def test_admin_sampling_survives_overrides_verbatim(self) -> None:
        """Таблица sampling профиля не трогается пользователем и уходит как есть."""
        overrides = UserLlmOverrides(history_messages=3)

        sampling = overrides.apply_to(_profile()).chat_sampling()

        if sampling != {"temperature": 0.1, "top_k": 40}:
            raise AssertionError(f"sampling: {sampling}")


class TestEdited:
    """Разбор формы: сравнение идёт с тем, что панель показала."""

    @staticmethod
    def _panel(saved: UserLlmOverrides, **kw) -> SettingsPanel:
        return _panel(_profile(**kw), saved)

    def test_untouched_form_stores_nothing(self) -> None:
        saved = UserLlmOverrides()
        panel = self._panel(saved)

        parsed = panel.parse(panel.shown_values()).overrides

        if parsed.stored() != {}:
            raise AssertionError(f"stored: {parsed.stored()}")

    def test_untouched_form_keeps_saved_override(self) -> None:
        saved = UserLlmOverrides(history_messages=7)
        panel = self._panel(saved)

        parsed = panel.parse(panel.shown_values()).overrides

        if parsed.stored() != {"history_messages": 7}:
            raise AssertionError(f"stored: {parsed.stored()}")

    def test_changed_value_is_stored(self) -> None:
        saved = UserLlmOverrides()
        panel = self._panel(saved)

        form = panel.shown_values()
        form[UserSetting.HISTORY_MESSAGES.value] = 7

        parsed = panel.parse(form).overrides

        if parsed.stored() != {"history_messages": 7}:
            raise AssertionError(f"stored: {parsed.stored()}")

    def test_profile_value_back_clears_override(self) -> None:
        saved = UserLlmOverrides(history_messages=7)
        panel = self._panel(saved)

        form = panel.shown_values()
        form[UserSetting.HISTORY_MESSAGES.value] = 25

        parsed = panel.parse(form).overrides

        if parsed.stored() != {}:
            raise AssertionError(f"stored: {parsed.stored()}")

    def test_empty_prompt_clears_the_instruction(self) -> None:
        saved = UserLlmOverrides(user_prompt="old")
        panel = self._panel(saved)

        form = panel.shown_values()
        form[UserSetting.USER_PROMPT.value] = "   "

        parsed = panel.parse(form).overrides

        if parsed.stored() != {}:
            raise AssertionError(f"stored: {parsed.stored()}")

    def test_prompt_is_stored_as_is(self) -> None:
        saved = UserLlmOverrides()
        panel = self._panel(saved)

        form = panel.shown_values()
        form[UserSetting.USER_PROMPT.value] = "be terse"

        parsed = panel.parse(form).overrides

        if parsed.stored() != {"user_prompt": "be terse"}:
            raise AssertionError(f"stored: {parsed.stored()}")


class TestSettingsPanel:
    @staticmethod
    def _widgets(tabs: Sequence[Tab]) -> dict[str, InputWidget]:
        found: dict[str, InputWidget] = {}
        for tab in tabs:
            for widget in tab.inputs:
                found[widget.id] = widget

        return found

    def test_tabs_cover_every_allowed_field(self) -> None:
        panel = _panel(_profile(), UserLlmOverrides())

        ids = set(self._widgets(panel.tabs()))

        expected = {setting.value for setting in UserSetting}
        if ids != expected:
            raise AssertionError(f"missing: {sorted(expected - ids)}")

    def test_tabs_are_named_and_ordered(self) -> None:
        panel = _panel(_profile(), UserLlmOverrides())

        ids = [tab.id for tab in panel.tabs()]

        if ids != [tab.value for tab in PanelTab]:
            raise AssertionError(f"tabs: {ids}")

    def test_every_widget_has_a_description(self) -> None:
        panel = _panel(_profile(), UserLlmOverrides())

        for name, widget in self._widgets(panel.tabs()).items():
            description = getattr(widget, "description", "")
            if not description:
                raise AssertionError(f"{name} has no description")

    def test_only_allowed_widgets_are_drawn(self) -> None:
        profile = _profile(settings=["history_messages", "user_prompt"])
        panel = _panel(profile, UserLlmOverrides())

        ids = set(self._widgets(panel.tabs()))

        if ids != {"history_messages", "user_prompt"}:
            raise AssertionError(f"ids: {sorted(ids)}")

    def test_no_tabs_for_empty_settings(self) -> None:
        panel = _panel(_profile(settings=[]), UserLlmOverrides())

        if panel.tabs() != []:
            raise AssertionError("tabs drawn for a closed profile")

    def _slider(self, panel: SettingsPanel, setting: UserSetting) -> Slider:
        widget = self._widgets(panel.tabs())[setting.value]
        if not isinstance(widget, Slider):
            raise AssertionError(f"{setting.value} is {type(widget).__name__}")

        return widget

    def test_history_slider_starts_at_profile_value(self) -> None:
        panel = _panel(_profile(), UserLlmOverrides())

        slider = self._slider(panel, UserSetting.HISTORY_MESSAGES)

        if (slider.min, slider.max, slider.step) != (1.0, 100.0, 1.0):
            raise AssertionError(f"bounds: {slider.min}, {slider.max}, {slider.step}")
        if slider.initial != 25:
            raise AssertionError(f"initial: {slider.initial}")

    def test_history_slider_starts_at_saved_override(self) -> None:
        panel = _panel(_profile(), UserLlmOverrides(history_messages=7))

        slider = self._slider(panel, UserSetting.HISTORY_MESSAGES)

        if slider.initial != 7:
            raise AssertionError(f"initial: {slider.initial}")

    def test_unknown_setting_name_is_config_error(self) -> None:
        with pytest.raises(ValueError, match="unknown settings"):
            _profile(settings=["temperature"])

    def test_enum_matches_override_fields(self) -> None:
        enum_names = {setting.value for setting in UserSetting}
        field_names = set(UserLlmOverrides.model_fields)
        if enum_names != field_names:
            raise AssertionError(f"mismatch: {enum_names ^ field_names}")

    def test_parse_drops_disallowed_fields(self) -> None:
        profile = _profile(settings=["history_messages"])
        saved = UserLlmOverrides()
        panel = _panel(profile, saved)

        form = panel.shown_values()
        form[UserSetting.HISTORY_MESSAGES.value] = 7
        form[UserSetting.USER_PROMPT.value] = "smuggled"

        parsed = panel.parse(form).overrides

        if parsed.stored() != {"history_messages": 7}:
            raise AssertionError(f"stored: {parsed.stored()}")

    def test_parse_clamps_out_of_bounds(self) -> None:
        panel = _panel(_profile(), UserLlmOverrides())

        form = panel.shown_values()
        form[UserSetting.HISTORY_MESSAGES.value] = 900

        parsed = panel.parse(form).overrides

        if parsed.stored() != {"history_messages": 100}:
            raise AssertionError(f"stored: {parsed.stored()}")


class TestPanelText:
    """Подписи панели: свой язык, откат на язык по умолчанию, полнота ключей."""

    def test_russian_strings_are_translated(self) -> None:
        text = PanelText(NO_OVERRIDES, "ru-RU")

        history = UserSetting.HISTORY_MESSAGES
        if text.label(history) != "Сообщений истории":
            raise AssertionError(f"label: {text.label(history)!r}")
        if text.tab(PanelTab.HISTORY.value) != "История":
            raise AssertionError(f"tab: {text.tab(PanelTab.HISTORY.value)!r}")

    def test_unknown_language_falls_back_to_default(self) -> None:
        text = PanelText(NO_OVERRIDES, "de-DE")

        history = UserSetting.HISTORY_MESSAGES
        if text.label(history) != "History messages":
            raise AssertionError(f"label: {text.label(history)!r}")

    def test_every_setting_is_translated(self) -> None:
        for language in ("en-US", "ru-RU"):
            text = PanelText(NO_OVERRIDES, language)

            for setting in UserSetting:
                if not text.label(setting):
                    raise AssertionError(f"{language}: no label for {setting.value}")
                if not text.description(setting):
                    raise AssertionError(f"{language}: no hint for {setting.value}")

            for tab in PanelTab:
                if not text.tab(tab.value):
                    raise AssertionError(f"{language}: no label for tab {tab.value}")

    def test_missing_key_is_reported(self) -> None:
        text = PanelText(NO_OVERRIDES, PanelText.DEFAULT_LANGUAGE)

        with pytest.raises(PanelTextError, match="no translation"):
            text.tab("no_such_tab")


class TestPanelLanguage:
    """Язык панели совпадает с языком остального интерфейса chainlit."""

    def test_forced_ui_language_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from chainlit.config import config as chainlit_config

        monkeypatch.setattr(chainlit_config.ui, "language", "ru-RU")

        if current_session().language != "ru-RU":
            raise AssertionError(f"language: {current_session().language!r}")

    def test_without_forced_language_falls_to_session(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from chainlit.config import config as chainlit_config

        monkeypatch.setattr(chainlit_config.ui, "language", None)

        # вне ws-сессии языка нет: панель возьмёт язык по умолчанию
        if current_session().language != "":
            raise AssertionError(f"language: {current_session().language!r}")


class TestUserMeta:
    def test_malformed_meta_degrades_to_empty(self) -> None:
        meta = UserMeta.of({"llm": {"general": {"history_messages": "abc"}}})

        if meta.llm != {}:
            raise AssertionError(f"llm: {meta.llm}")

    def test_missing_profile_gives_empty_overrides(self) -> None:
        meta = UserMeta.of({"llm": {"general": {"history_messages": 5}}})

        if meta.overrides_for("search").stored() != {}:
            raise AssertionError("search overrides are not empty")
        if meta.overrides_for("general").history_messages != 5:
            raise AssertionError("general override lost")

    def test_stale_sampling_keys_are_ignored(self) -> None:
        """users.meta прошлых версий с сэмплингом: лишние ключи отбрасываются."""
        meta = UserMeta.of(
            {"llm": {"general": {"temperature": 0.9, "history_messages": 5}}}
        )

        overrides = meta.overrides_for("general")
        if overrides.stored() != {"history_messages": 5}:
            raise AssertionError(f"stored: {overrides.stored()}")


class TestUserSettingsStorage:
    """Хранение в users.meta: переживает логин, снимается пустым значением."""

    @staticmethod
    async def _meta_of(
        pool: AsyncPostgresPool,
        schema: str,
        identifier: str,
    ) -> dict:
        """Сырой users.meta: тест сверяет и настройки, и соседние ключи."""
        query = sql.SQL("select meta from {}.users where identifier = %s").format(
            sql.Identifier(schema)
        )
        async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(query, (identifier,))
            row = await cur.fetchone()

        if row is None:
            raise AssertionError(f"user {identifier} is not stored")

        return dict(row["meta"])

    async def test_settings_survive_relogin(
        self,
        layer: PostgresDataLayer,
        pool: AsyncPostgresPool,
        app_config: AppConfig,
    ) -> None:
        identifier = f"settings-user-{uuid4()}"
        created = await layer.create_user(
            ChainlitUser(identifier=identifier, metadata={"roles": ["DEV"]})
        )
        if created is None:
            raise AssertionError("user is not created")

        await layer.update_user_llm_settings(
            UUID(created.id), "general", {"history_messages": 5}
        )

        # повторный логин: провайдер приносит новую мету
        again = await layer.create_user(
            ChainlitUser(identifier=identifier, metadata={"roles": ["ADM"]})
        )
        if again is None:
            raise AssertionError("relogin failed")

        meta = await self._meta_of(pool, app_config.data_layer.db_schema, identifier)
        if meta.get("roles") != ["ADM"]:
            raise AssertionError(f"roles: {meta.get('roles')}")
        if meta.get("llm") != {"general": {"history_messages": 5}}:
            raise AssertionError(f"llm: {meta.get('llm')}")

    async def test_empty_values_remove_the_profile_key(
        self,
        layer: PostgresDataLayer,
        pool: AsyncPostgresPool,
        app_config: AppConfig,
    ) -> None:
        identifier = f"settings-user-{uuid4()}"
        created = await layer.create_user(ChainlitUser(identifier=identifier))
        if created is None:
            raise AssertionError("user is not created")

        await layer.update_user_llm_settings(
            UUID(created.id), "general", {"history_messages": 5}
        )
        await layer.update_user_llm_settings(UUID(created.id), "general", {})

        meta = await self._meta_of(pool, app_config.data_layer.db_schema, identifier)
        if meta.get("llm", {}).get("general") is not None:
            raise AssertionError(f"llm: {meta.get('llm')}")

    async def test_profiles_are_isolated(
        self,
        layer: PostgresDataLayer,
        pool: AsyncPostgresPool,
        app_config: AppConfig,
    ) -> None:
        identifier = f"settings-user-{uuid4()}"
        created = await layer.create_user(ChainlitUser(identifier=identifier))
        if created is None:
            raise AssertionError("user is not created")

        await layer.update_user_llm_settings(
            UUID(created.id), "general", {"history_messages": 5}
        )
        await layer.update_user_llm_settings(
            UUID(created.id), "search", {"user_prompt": "be terse"}
        )

        meta = await self._meta_of(pool, app_config.data_layer.db_schema, identifier)
        expected = {
            "general": {"history_messages": 5},
            "search": {"user_prompt": "be terse"},
        }
        if meta.get("llm") != expected:
            raise AssertionError(f"llm: {meta.get('llm')}")
