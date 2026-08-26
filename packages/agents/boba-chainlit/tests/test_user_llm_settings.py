"""Тесты слоя пользовательских настроек LLM: границы, наложение, хранение."""

from __future__ import annotations

from collections.abc import Sequence
from uuid import uuid4

import pytest
from chainlit.input_widget import InputWidget, Select, Slider, Tab
from chainlit.user import User as ChainlitUser
from psycopg import sql
from psycopg.rows import dict_row

from boba.chainlit.chat.panel_text import PanelText, PanelTextError
from boba.chainlit.chat.settings import PanelTab, SettingsPanel
from boba.chainlit.data.data_layer import PostgresDataLayer
from boba.chainlit.infra.config import (
    AppConfig,
    ChatProfileConfig,
    ReasoningEffort,
    SettingsBounds,
    SettingsView,
    UserLlmOverrides,
    UserMeta,
    UserSetting,
)
from boba.chainlit.infra.session import current_session
from boba.db.postgres import AsyncPostgresPool

pytestmark = pytest.mark.anyio

from boba.chat.provider import OpenAiChatConfig

OPENAI = {"base_url": "https://llm.example/v1", "api_key": "token"}

BACKEND = {"provider": "openai", "openai": OPENAI}

NO_OVERRIDES = ""
"""Пустой app_root: строки берутся из пакета, как в чистом развёртывании."""


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    """Заглушка сессионной фикстуры: тесты слоя не ходят в контекст chainlit."""


BOUNDS = SettingsBounds.model_validate(
    {
        "temperature": {"min": 0.0, "max": 2.0, "step": 0.05, "default": 1.0},
        "top_p": {"min": 0.0, "max": 1.0, "step": 0.05, "default": 1.0},
        "max_tokens": {"min": 256, "max": 16000, "step": 256, "default": 4096},
        "frequency_penalty": {"min": -2.0, "max": 2.0, "step": 0.1, "default": 0.0},
        "presence_penalty": {"min": -2.0, "max": 2.0, "step": 0.1, "default": 0.0},
        "history_messages": {"min": 1, "max": 100, "step": 1, "default": 30},
    }
)


def _panel(profile: ChatProfileConfig, saved: UserLlmOverrides) -> SettingsPanel:
    view = SettingsView.of(BOUNDS, profile, saved)
    return SettingsPanel(view, PanelText(NO_OVERRIDES, PanelText.DEFAULT_LANGUAGE))


def _profile(**kw) -> ChatProfileConfig:
    base = {
        "display_name": "Profile",
        "description": "test profile",
        "backend": BACKEND,
        "model": "base-model",
        "models": ["base-model", "alt-model"],
        "settings": ["*"],
        "system_prompt": "You are the profile assistant",
        "temperature": 0.1,
        "max_tokens": 1000,
    }
    base.update(kw)
    return ChatProfileConfig.model_validate(base)


class TestClamped:
    def test_values_are_clamped_into_bounds(self) -> None:
        overrides = UserLlmOverrides(temperature=9.0, max_tokens=1, top_p=-1.0)

        bounded = overrides.clamped(BOUNDS, _profile())

        if bounded.temperature != 2.0:
            raise AssertionError(f"temperature: {bounded.temperature}")
        if bounded.max_tokens != 256:
            raise AssertionError(f"max_tokens: {bounded.max_tokens}")
        if bounded.top_p != 0.0:
            raise AssertionError(f"top_p: {bounded.top_p}")

    def test_foreign_model_is_dropped(self) -> None:
        overrides = UserLlmOverrides(model="smuggled-model")

        bounded = overrides.clamped(BOUNDS, _profile())

        if bounded.model is not None:
            raise AssertionError(f"model survived: {bounded.model}")

    def test_listed_model_is_kept(self) -> None:
        overrides = UserLlmOverrides(model="alt-model")

        bounded = overrides.clamped(BOUNDS, _profile())

        if bounded.model != "alt-model":
            raise AssertionError(f"model: {bounded.model}")

    def test_none_fields_stay_none(self) -> None:
        bounded = UserLlmOverrides().clamped(BOUNDS, _profile())

        if bounded.stored() != {}:
            raise AssertionError(f"stored: {bounded.stored()}")

    def test_disallowed_setting_is_dropped(self) -> None:
        profile = _profile(settings=["top_p"])
        overrides = UserLlmOverrides(temperature=0.9, top_p=0.5)

        bounded = overrides.clamped(BOUNDS, profile)

        if bounded.stored() != {"top_p": 0.5}:
            raise AssertionError(f"stored: {bounded.stored()}")

    def test_empty_settings_disallow_everything(self) -> None:
        profile = _profile(settings=[])
        overrides = UserLlmOverrides(temperature=0.9, user_prompt="sneak")

        bounded = overrides.clamped(BOUNDS, profile)

        if bounded.stored() != {}:
            raise AssertionError(f"stored: {bounded.stored()}")


class TestApplyTo:
    def test_override_wins_over_profile(self) -> None:
        overrides = UserLlmOverrides(temperature=0.9, model="alt-model")

        settings = overrides.apply_to(_profile())

        if settings.temperature != 0.9:
            raise AssertionError(f"temperature: {settings.temperature}")
        if settings.model != "alt-model":
            raise AssertionError(f"model: {settings.model}")

    def test_none_keeps_profile_value(self) -> None:
        settings = UserLlmOverrides().apply_to(_profile())

        if settings.temperature != 0.1:
            raise AssertionError(f"temperature: {settings.temperature}")
        if settings.model != "base-model":
            raise AssertionError(f"model: {settings.model}")

    def test_user_prompt_is_appended(self) -> None:
        overrides = UserLlmOverrides(user_prompt="Answer in Russian")

        settings = overrides.apply_to(_profile())

        expected = "You are the profile assistant\n\nAnswer in Russian"
        if settings.system_prompt != expected:
            raise AssertionError(f"system_prompt: {settings.system_prompt!r}")

    def test_transport_is_never_overridden(self) -> None:
        settings = UserLlmOverrides(temperature=1.0).apply_to(_profile())

        backend = settings.backend
        if not isinstance(backend, OpenAiChatConfig):
            raise AssertionError(f"backend is openai: {backend}")
        if backend.openai.base_url != OPENAI["base_url"]:
            raise AssertionError("openai transport changed")

    def test_reasoning_and_seed_reach_chat_sampling(self) -> None:
        overrides = UserLlmOverrides(seed=7, reasoning_effort=ReasoningEffort.HIGH)

        sampling = overrides.apply_to(_profile()).chat_sampling()

        if sampling.seed != 7:
            raise AssertionError(f"seed: {sampling.seed}")
        if sampling.reasoning_effort != "high":
            raise AssertionError(f"effort: {sampling.reasoning_effort}")


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
        saved = UserLlmOverrides(temperature=0.9)
        panel = self._panel(saved)

        parsed = panel.parse(panel.shown_values()).overrides

        if parsed.stored() != {"temperature": 0.9}:
            raise AssertionError(f"stored: {parsed.stored()}")

    def test_changed_value_is_stored(self) -> None:
        saved = UserLlmOverrides()
        panel = self._panel(saved)

        form = panel.shown_values()
        form[UserSetting.TEMPERATURE.value] = 0.9

        parsed = panel.parse(form).overrides

        if parsed.stored() != {"temperature": 0.9}:
            raise AssertionError(f"stored: {parsed.stored()}")

    def test_profile_value_back_clears_override(self) -> None:
        saved = UserLlmOverrides(temperature=0.9)
        panel = self._panel(saved)

        form = panel.shown_values()
        form[UserSetting.TEMPERATURE.value] = 0.1

        parsed = panel.parse(form).overrides

        if parsed.stored() != {}:
            raise AssertionError(f"stored: {parsed.stored()}")

    def test_parameter_absent_in_profile_is_not_stored_untouched(self) -> None:
        """Слайдер показал стартовое значение из [settings], а не значение профиля."""
        saved = UserLlmOverrides()
        panel = self._panel(saved)

        shown = panel.shown_values()
        if shown[UserSetting.TOP_P.value] != BOUNDS.top_p.default:
            raise AssertionError(f"shown top_p: {shown[UserSetting.TOP_P.value]}")

        parsed = panel.parse(shown).overrides

        if parsed.stored() != {}:
            raise AssertionError(f"stored: {parsed.stored()}")

    def test_moved_slider_of_absent_parameter_is_stored(self) -> None:
        saved = UserLlmOverrides()
        panel = self._panel(saved)

        form = panel.shown_values()
        form[UserSetting.TOP_P.value] = 0.55

        parsed = panel.parse(form).overrides

        if parsed.stored() != {"top_p": 0.55}:
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
        profile = _profile(settings=["temperature", "user_prompt"])
        panel = _panel(profile, UserLlmOverrides())

        ids = set(self._widgets(panel.tabs()))

        if ids != {"temperature", "user_prompt"}:
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

    def _select(self, panel: SettingsPanel, setting: UserSetting) -> Select:
        widget = self._widgets(panel.tabs())[setting.value]
        if not isinstance(widget, Select):
            raise AssertionError(f"{setting.value} is {type(widget).__name__}")

        return widget

    def test_slider_starts_at_profile_value(self) -> None:
        panel = _panel(_profile(), UserLlmOverrides())

        slider = self._slider(panel, UserSetting.TEMPERATURE)

        if (slider.min, slider.max, slider.step) != (0.0, 2.0, 0.05):
            raise AssertionError(f"bounds: {slider.min}, {slider.max}, {slider.step}")
        if slider.initial != 0.1:
            raise AssertionError(f"initial: {slider.initial}")

    def test_slider_starts_at_settings_default_when_profile_is_silent(self) -> None:
        panel = _panel(_profile(), UserLlmOverrides())

        slider = self._slider(panel, UserSetting.TOP_P)

        if slider.initial != BOUNDS.top_p.default:
            raise AssertionError(f"initial: {slider.initial}")

    def test_slider_starts_at_saved_override(self) -> None:
        panel = _panel(_profile(), UserLlmOverrides(temperature=1.4))

        slider = self._slider(panel, UserSetting.TEMPERATURE)

        if slider.initial != 1.4:
            raise AssertionError(f"initial: {slider.initial}")

    def test_model_select_lists_profile_models(self) -> None:
        panel = _panel(_profile(), UserLlmOverrides())

        select = self._select(panel, UserSetting.MODEL)

        values = [item["value"] for item in select.to_dict()["items"]]
        if values != ["base-model", "alt-model"]:
            raise AssertionError(f"values: {values}")
        if select.initial != "base-model":
            raise AssertionError(f"initial: {select.initial}")

    def test_effort_is_a_select_of_levels(self) -> None:
        panel = _panel(_profile(), UserLlmOverrides())

        select = self._select(panel, UserSetting.REASONING_EFFORT)

        values = [item["value"] for item in select.to_dict()["items"]]
        if values != [level.value for level in ReasoningEffort]:
            raise AssertionError(f"values: {values}")

    def test_no_model_select_without_whitelist(self) -> None:
        profile = _profile(models=[], settings=["temperature"])
        panel = _panel(profile, UserLlmOverrides())

        ids = set(self._widgets(panel.tabs()))

        if UserSetting.MODEL.value in ids:
            raise AssertionError("model select is drawn without a whitelist")

    def test_allowed_model_requires_whitelist(self) -> None:
        with pytest.raises(ValueError, match="models"):
            _profile(models=[], settings=["*"])

    def test_unknown_setting_name_is_config_error(self) -> None:
        with pytest.raises(ValueError, match="unknown settings"):
            _profile(settings=["tempreture"])

    def test_enum_matches_override_fields(self) -> None:
        enum_names = {setting.value for setting in UserSetting}
        field_names = set(UserLlmOverrides.model_fields)
        if enum_names != field_names:
            raise AssertionError(f"mismatch: {enum_names ^ field_names}")

    def test_parse_drops_disallowed_fields(self) -> None:
        profile = _profile(settings=["temperature"])
        saved = UserLlmOverrides()
        panel = _panel(profile, saved)

        form = panel.shown_values()
        form[UserSetting.TEMPERATURE.value] = 0.9
        form[UserSetting.USER_PROMPT.value] = "smuggled"

        parsed = panel.parse(form).overrides

        if parsed.stored() != {"temperature": 0.9}:
            raise AssertionError(f"stored: {parsed.stored()}")

    def test_parse_clamps_out_of_bounds(self) -> None:
        panel = _panel(_profile(), UserLlmOverrides())

        form = panel.shown_values()
        form[UserSetting.TEMPERATURE.value] = 5.0

        parsed = panel.parse(form).overrides

        if parsed.stored() != {"temperature": 2.0}:
            raise AssertionError(f"stored: {parsed.stored()}")


class TestPanelText:
    """Подписи панели: свой язык, откат на язык по умолчанию, полнота ключей."""

    def test_russian_strings_are_translated(self) -> None:
        text = PanelText(NO_OVERRIDES, "ru-RU")

        if text.label(UserSetting.TEMPERATURE) != "Температура":
            raise AssertionError(f"label: {text.label(UserSetting.TEMPERATURE)!r}")
        if text.tab(PanelTab.SAMPLING.value) != "Сэмплинг":
            raise AssertionError(f"tab: {text.tab(PanelTab.SAMPLING.value)!r}")

    def test_unknown_language_falls_back_to_default(self) -> None:
        text = PanelText(NO_OVERRIDES, "de-DE")

        if text.label(UserSetting.TEMPERATURE) != "Temperature":
            raise AssertionError(f"label: {text.label(UserSetting.TEMPERATURE)!r}")

    def test_base_language_is_used(self) -> None:
        text = PanelText(NO_OVERRIDES, "ru")

        if text.label(UserSetting.SEED) != "Зерно":
            raise AssertionError(f"label: {text.label(UserSetting.SEED)!r}")

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
        meta = UserMeta.of({"llm": {"general": {"temperature": "abc"}}})

        if meta.llm != {}:
            raise AssertionError(f"llm: {meta.llm}")

    def test_missing_profile_gives_empty_overrides(self) -> None:
        meta = UserMeta.of({"llm": {"general": {"temperature": 0.5}}})

        if meta.overrides_for("search").stored() != {}:
            raise AssertionError("search overrides are not empty")
        if meta.overrides_for("general").temperature != 0.5:
            raise AssertionError("general override lost")


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
            int(created.id), "general", {"temperature": 0.9}
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
        if meta.get("llm") != {"general": {"temperature": 0.9}}:
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
            int(created.id), "general", {"temperature": 0.9}
        )
        await layer.update_user_llm_settings(int(created.id), "general", {})

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
            int(created.id), "general", {"temperature": 0.9}
        )
        await layer.update_user_llm_settings(
            int(created.id), "search", {"model": "alt-model"}
        )

        meta = await self._meta_of(pool, app_config.data_layer.db_schema, identifier)
        expected = {
            "general": {"temperature": 0.9},
            "search": {"model": "alt-model"},
        }
        if meta.get("llm") != expected:
            raise AssertionError(f"llm: {meta.get('llm')}")
