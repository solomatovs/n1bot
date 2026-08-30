"""Панель настроек LLM в браузере: каждый виджет, users.meta и запрос к LLM.

На каждую настройку — свой прогон: состояние виджета до и после ввода, Confirm,
содержимое users.meta.llm и переопределение профильного значения в теле запроса
к fake llm. Ожидаемые значения профилей заданы стендом
(StandConfig._use_test_profiles).
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path as FilePath
from typing import Any

import httpx
import pytest
from chat_ui import LlmMetaReader
from playwright.sync_api import expect

from boba.chainlit.chat.panel_text import PanelText
from boba.chainlit.chat.settings import PanelTab
from boba.chat.profiles import UserSetting
from boba.stand.ui.chat_page import ChatPage, StepKind
from boba.stand.ui.fake_llm import ScenarioName
from boba.stand.ui.stand import StandProcess, StandUrl

pytestmark = pytest.mark.ui

ADMIN_LOGIN = "admin"
GENERAL_PROMPT = "You are the general stand assistant"
SEARCH_PROMPT = "You are the search stand assistant"


def _app_language() -> str:
    """Язык интерфейса: его навязывает секция [UI] конфига chainlit.

    Подписи панели тест ищет на этом языке; иначе он ждёт английские названия
    вкладок, а панель нарисована на языке развёртывания.
    """
    root = os.environ.get("BOBA_APP_ROOT")
    if not root:
        root = f"{os.environ.get('BOBA_BASE', '')}/app_root"

    config = FilePath(root) / ".chainlit" / "config.toml"
    if not config.is_file():
        return PanelText.DEFAULT_LANGUAGE

    doc = tomllib.loads(config.read_text(encoding="utf-8"))
    language = doc.get("UI", {}).get("language")
    if not language:
        return PanelText.DEFAULT_LANGUAGE

    return str(language)


TEXT = PanelText("", _app_language())
"""Подписи панели на языке приложения."""

SAVE_GRACE_MS = 1500
"""Пауза после Confirm: сохранение и сброс DI-контейнера идут по сокету."""


class PanelSelector(StrEnum):
    """Селекторы модала настроек chainlit."""

    OPEN_MODAL = "#chat-settings-open-modal"
    MODAL = "#chat-settings"
    CONFIRM = "#confirm"
    TAB = "[role='tab']"

    @staticmethod
    def input_of(field: str) -> str:
        return f"input#{field}"

    @staticmethod
    def textarea_of(field: str) -> str:
        return f"textarea#{field}"

    @staticmethod
    def trigger_of(field: str) -> str:
        return f"button#{field}"

    @staticmethod
    def slider_of(field: str) -> str:
        return f"#{field}[role='slider'], #{field} [role='slider']"


class Action(StrEnum):
    """Способ ввода значения в виджет."""

    NUMBER = "number"
    TEXT = "text"
    SELECT = "select"
    TAGS = "tags"
    SLIDER = "slider"


@dataclass(frozen=True)
class FieldCase:
    """Одна настройка: вкладка, жест ввода и ожидания по итогу."""

    setting: UserSetting
    tab: PanelTab
    action: Action
    value: str = ""
    """Для number/text/select/tags/radio; слайдер шагает клавишей."""

    meta: dict[str, Any] | None = None
    """Ожидаемое users.meta.llm.general; None — сверяется со снимком виджета."""

    payload_key: str = ""
    payload_value: Any = None
    integer: bool = False
    """Слайдер целочисленной настройки: в meta уходит int, не float."""


CASES = (
    FieldCase(
        setting=UserSetting.TEMPERATURE,
        tab=PanelTab.SAMPLING,
        action=Action.SLIDER,
        payload_key="temperature",
    ),
    FieldCase(
        setting=UserSetting.TOP_P,
        tab=PanelTab.SAMPLING,
        action=Action.SLIDER,
        payload_key="top_p",
    ),
    FieldCase(
        setting=UserSetting.MAX_TOKENS,
        tab=PanelTab.SAMPLING,
        action=Action.SLIDER,
        payload_key="max_completion_tokens",
        integer=True,
    ),
    FieldCase(
        setting=UserSetting.FREQUENCY_PENALTY,
        tab=PanelTab.SAMPLING,
        action=Action.SLIDER,
        payload_key="frequency_penalty",
    ),
    FieldCase(
        setting=UserSetting.PRESENCE_PENALTY,
        tab=PanelTab.SAMPLING,
        action=Action.SLIDER,
        payload_key="presence_penalty",
    ),
    FieldCase(
        setting=UserSetting.HISTORY_MESSAGES,
        tab=PanelTab.SAMPLING,
        action=Action.SLIDER,
        integer=True,
    ),
    FieldCase(
        setting=UserSetting.STOP,
        tab=PanelTab.SAMPLING,
        action=Action.TAGS,
        value="END",
        meta={"stop": ["END"]},
        payload_key="stop",
        payload_value=["END"],
    ),
    FieldCase(
        setting=UserSetting.SEED,
        tab=PanelTab.MODEL,
        action=Action.NUMBER,
        value="42",
        meta={"seed": 42},
        payload_key="seed",
        payload_value=42,
    ),
    FieldCase(
        setting=UserSetting.MODEL,
        tab=PanelTab.MODEL,
        action=Action.SELECT,
        value="fake-model-alt",
        meta={"model": "fake-model-alt"},
        payload_key="model",
        payload_value="fake-model-alt",
    ),
    FieldCase(
        setting=UserSetting.REASONING_EFFORT,
        tab=PanelTab.MODEL,
        action=Action.SELECT,
        value="high",
        meta={"reasoning_effort": "high"},
        payload_key="reasoning_effort",
        payload_value="high",
    ),
    FieldCase(
        setting=UserSetting.USER_PROMPT,
        tab=PanelTab.PROMPT,
        action=Action.TEXT,
        value="Always answer in haiku",
        meta={"user_prompt": "Always answer in haiku"},
    ),
)


@pytest.fixture(autouse=True)
def _fresh_settings(clean_llm_settings: None) -> None:
    """Каждый тест начинает без сохранённых настроек прошлых тестов."""


def _llm_requests(llm_port: int) -> list[dict[str, Any]]:
    response = httpx.get(StandUrl.of(llm_port, "/requests"), timeout=5.0)
    response.raise_for_status()
    return response.json()["requests"]


def _last_request(llm_port: int) -> dict[str, Any]:
    requests = _llm_requests(llm_port)
    if not requests:
        raise AssertionError("fake llm got no requests")

    return requests[-1]


def _system_prompt(payload: dict[str, Any]) -> str:
    messages = payload.get("messages", [])
    if not messages:
        raise AssertionError("request has no messages")

    first = messages[0]
    if first.get("role") != "system":
        raise AssertionError(f"first message is not system: {first}")

    return str(first.get("content", ""))


def _open_settings(page: ChatPage) -> None:
    page.page.locator(PanelSelector.OPEN_MODAL.value).click()
    page.page.wait_for_selector(PanelSelector.TAB.value, timeout=15000)


def _open_tab(page: ChatPage, tab: PanelTab) -> None:
    page.page.get_by_role("tab", name=TEXT.tab(tab.value), exact=True).click()
    page.page.wait_for_timeout(300)


def _confirm_settings(page: ChatPage) -> None:
    page.page.locator(PanelSelector.CONFIRM.value).click()
    page.page.wait_for_timeout(SAVE_GRACE_MS)


def _ask_and_wait(page: ChatPage) -> None:
    page.ask(f"{ScenarioName.ANSWER.value} please")
    page.await_step(StepKind.ASSISTANT.value)
    page.await_idle()


def _widget_state(page: ChatPage, case: FieldCase) -> str:
    """Снимок состояния виджета: тест сверяет его до и после ввода."""
    field = case.setting.value

    if case.action in (Action.NUMBER, Action.TAGS):
        return page.page.locator(PanelSelector.input_of(field)).input_value()

    if case.action is Action.TEXT:
        return page.page.locator(PanelSelector.textarea_of(field)).input_value()

    if case.action is Action.SELECT:
        return page.page.locator(PanelSelector.trigger_of(field)).inner_text()

    slider = page.page.locator(PanelSelector.slider_of(field)).first
    value = slider.get_attribute("aria-valuenow")
    if value is None:
        raise AssertionError(f"slider {field} has no aria-valuenow")

    return value


def _enter_value(page: ChatPage, case: FieldCase) -> None:
    """Ввод значения тем жестом, которым пользуется человек."""
    field = case.setting.value

    if case.action is Action.NUMBER:
        page.page.fill(PanelSelector.input_of(field), case.value)
        return

    if case.action is Action.TEXT:
        page.page.fill(PanelSelector.textarea_of(field), case.value)
        return

    if case.action is Action.TAGS:
        tags = page.page.locator(PanelSelector.input_of(field))
        tags.fill(case.value)
        tags.press("Enter")
        return

    if case.action is Action.SELECT:
        page.page.locator(PanelSelector.trigger_of(field)).click()
        page.page.get_by_role("option", name=case.value, exact=True).click()
        return

    slider = page.page.locator(PanelSelector.slider_of(field)).first
    slider.click()
    slider.press("ArrowRight")
    page.page.wait_for_timeout(200)


def _expected_meta(case: FieldCase, state: str) -> dict[str, Any]:
    """Что обязано оказаться в users.meta после сохранения."""
    if case.meta is not None:
        return case.meta

    if case.integer:
        return {case.setting.value: int(float(state))}

    return {case.setting.value: float(state)}


class TestEveryFieldClickable:
    """Каждый виджет панели: состояние до ввода, после ввода и после сохранения."""

    @pytest.mark.parametrize("case", CASES, ids=lambda case: case.setting.value)
    def test_field_saves_and_overrides(
        self,
        chat: ChatPage,
        llm_port: int,
        llm_meta: LlmMetaReader,
        case: FieldCase,
    ) -> None:
        _open_settings(chat)
        _open_tab(chat, case.tab)

        before = _widget_state(chat, case)

        _enter_value(chat, case)

        after = _widget_state(chat, case)
        if case.action is Action.TAGS:
            # поле тегов очищается после Enter, введённое становится бейджем
            badge = chat.page.locator(
                f"{PanelSelector.MODAL.value} >> text={case.value}"
            )
            if not badge.count():
                raise AssertionError(f"{case.value} tag badge is not drawn")
        elif after == before:
            raise AssertionError(
                f"{case.setting.value}: DOM did not change ({before!r})"
            )

        _confirm_settings(chat)

        saved = llm_meta(ADMIN_LOGIN)
        expected = _expected_meta(case, after)
        stored = saved.get("general")
        if stored != expected:
            raise AssertionError(f"meta.llm.general: {stored} != {expected}")

        _ask_and_wait(chat)
        payload = _last_request(llm_port)

        if case.payload_key:
            wanted = case.payload_value
            if wanted is None:
                wanted = expected[case.setting.value]

            sent = payload.get(case.payload_key)
            if sent != wanted:
                raise AssertionError(f"{case.payload_key}: {sent!r} != {wanted!r}")

        if case.setting is UserSetting.USER_PROMPT:
            prompt = _system_prompt(payload)
            if prompt != f"{GENERAL_PROMPT}\n\n{case.value}":
                raise AssertionError(f"system prompt: {prompt!r}")


class TestProfileValueBack:
    """Возврат значения к профильному снимает переопределение."""

    def test_model_back_to_profile_clears_override(
        self, chat: ChatPage, llm_port: int, llm_meta: LlmMetaReader
    ) -> None:
        _open_settings(chat)
        _open_tab(chat, PanelTab.MODEL)

        chat.page.locator(PanelSelector.trigger_of(UserSetting.MODEL.value)).click()
        chat.page.get_by_role("option", name="fake-model-alt", exact=True).click()
        _confirm_settings(chat)

        if llm_meta(ADMIN_LOGIN).get("general") != {"model": "fake-model-alt"}:
            raise AssertionError("model override is not saved")

        _open_settings(chat)
        _open_tab(chat, PanelTab.MODEL)
        chat.page.locator(PanelSelector.trigger_of(UserSetting.MODEL.value)).click()
        chat.page.get_by_role("option", name="fake-model-general", exact=True).click()
        _confirm_settings(chat)

        if llm_meta(ADMIN_LOGIN).get("general") is not None:
            raise AssertionError(f"model stayed stored: {llm_meta(ADMIN_LOGIN)}")

        _ask_and_wait(chat)
        payload = _last_request(llm_port)
        if payload.get("model") != "fake-model-general":
            raise AssertionError(f"model: {payload.get('model')!r}")

    def test_untouched_panel_stores_nothing(
        self, chat: ChatPage, llm_port: int, llm_meta: LlmMetaReader
    ) -> None:
        _open_settings(chat)
        _confirm_settings(chat)

        if llm_meta(ADMIN_LOGIN).get("general") is not None:
            raise AssertionError(f"stored without edits: {llm_meta(ADMIN_LOGIN)}")

        _ask_and_wait(chat)
        payload = _last_request(llm_port)

        # профиль не задаёт penalties: стартовые значения слайдеров в запрос
        # не уходят, а заданные профилем остаются его значениями
        for absent in ("frequency_penalty", "presence_penalty", "seed"):
            if absent in payload:
                raise AssertionError(f"{absent} leaked: {payload.get(absent)!r}")

        if payload.get("temperature") != 0.1:
            raise AssertionError(f"temperature: {payload.get('temperature')!r}")
        if payload.get("top_p") != 0.9:
            raise AssertionError(f"top_p: {payload.get('top_p')!r}")


class TestWidgetDescriptions:
    """У каждой настройки в панели есть пояснение."""

    def test_every_widget_shows_a_description(self, chat: ChatPage) -> None:
        _open_settings(chat)

        for tab in PanelTab:
            _open_tab(chat, tab)

            panel = chat.page.locator(PanelSelector.MODAL.value)
            text = panel.inner_text()
            for case in CASES:
                if case.tab is not tab:
                    continue

                hint = TEXT.description(case.setting).split(";")[0].split(":")[0]
                if hint[:40] not in text:
                    raise AssertionError(
                        f"{case.setting.value}: description is not shown"
                    )


class TestAllowedSettingsVisibility:
    """Панель показывает только вкладки и виджеты, разрешённые профилем."""

    def test_general_shows_every_tab(self, chat: ChatPage) -> None:
        _open_settings(chat)

        labels = set()
        tabs = chat.page.locator(PanelSelector.TAB.value)
        for index in range(tabs.count()):
            labels.add(tabs.nth(index).inner_text().strip())

        expected = {TEXT.tab(tab.value) for tab in PanelTab}
        if labels != expected:
            raise AssertionError(f"tabs: {sorted(labels)}")

    def test_general_shows_every_widget(self, chat: ChatPage) -> None:
        _open_settings(chat)

        for case in CASES:
            _open_tab(chat, case.tab)

            field = case.setting.value
            if case.action in (Action.NUMBER, Action.TAGS):
                selector = PanelSelector.input_of(field)
            elif case.action is Action.TEXT:
                selector = PanelSelector.textarea_of(field)
            elif case.action is Action.SELECT:
                selector = PanelSelector.trigger_of(field)
            else:
                selector = PanelSelector.slider_of(field)

            if not chat.page.locator(selector).count():
                raise AssertionError(f"widget {field} is not drawn")

    def test_search_shows_only_allowed_widgets(self, chat: ChatPage) -> None:
        chat.open_profile_menu()
        chat.select_profile("search")

        _open_settings(chat)

        labels = set()
        tabs = chat.page.locator(PanelSelector.TAB.value)
        for index in range(tabs.count()):
            labels.add(tabs.nth(index).inner_text().strip())

        # search разрешает temperature, top_p, history_messages и user_prompt
        expected = {
            TEXT.tab(PanelTab.SAMPLING.value),
            TEXT.tab(PanelTab.PROMPT.value),
        }
        if labels != expected:
            raise AssertionError(f"tabs: {sorted(labels)}")

        _open_tab(chat, PanelTab.SAMPLING)
        allowed = PanelSelector.slider_of(UserSetting.TEMPERATURE.value)
        if not chat.page.locator(allowed).count():
            raise AssertionError("temperature slider is not drawn")

        forbidden = {
            UserSetting.MAX_TOKENS.value: PanelSelector.slider_of(
                UserSetting.MAX_TOKENS.value
            ),
            UserSetting.STOP.value: PanelSelector.input_of(UserSetting.STOP.value),
        }
        for name, selector in forbidden.items():
            if chat.page.locator(selector).count():
                raise AssertionError(f"forbidden widget {name} is drawn")


class TestSettingsLifecycle:
    def test_settings_survive_reload(self, chat: ChatPage) -> None:
        _open_settings(chat)
        _open_tab(chat, PanelTab.PROMPT)
        chat.page.fill(
            PanelSelector.textarea_of(UserSetting.USER_PROMPT.value), "stay here"
        )
        _confirm_settings(chat)

        chat.page.reload(wait_until="domcontentloaded")
        chat.page.wait_for_selector("#chat-input", timeout=60000)
        chat.page.wait_for_timeout(1000)

        _open_settings(chat)
        _open_tab(chat, PanelTab.PROMPT)
        value = chat.page.locator(
            PanelSelector.textarea_of(UserSetting.USER_PROMPT.value)
        ).input_value()
        if value != "stay here":
            raise AssertionError(f"user_prompt after reload: {value!r}")

    def test_out_of_bounds_value_is_clamped(
        self, chat: ChatPage, llm_port: int, llm_meta: LlmMetaReader
    ) -> None:
        _open_settings(chat)
        _open_tab(chat, PanelTab.MODEL)
        chat.page.fill(PanelSelector.input_of(UserSetting.SEED.value), "-5")
        _confirm_settings(chat)

        # seed отрицательным быть не может: модель отвергает значение целиком
        if llm_meta(ADMIN_LOGIN).get("general") is not None:
            raise AssertionError(f"negative seed stored: {llm_meta(ADMIN_LOGIN)}")


class TestSettingsAfterTurn:
    """Панель настроек остаётся рабочей, когда в треде уже есть переписка."""

    def test_panel_opens_after_the_first_message(self, chat: ChatPage) -> None:
        _ask_and_wait(chat)

        _open_settings(chat)
        _open_tab(chat, PanelTab.PROMPT)

        field = PanelSelector.textarea_of(UserSetting.USER_PROMPT.value)
        if chat.page.locator(field).count() == 0:
            raise AssertionError("после сообщения панель открылась без виджетов")


class TestSettingsIsolation:
    """Настройки general не текут в search: у каждого профиля свои."""

    def test_other_profile_keeps_its_own_settings(
        self, chat: ChatPage, llm_port: int, llm_meta: LlmMetaReader
    ) -> None:
        _open_settings(chat)
        _open_tab(chat, PanelTab.PROMPT)
        chat.page.fill(
            PanelSelector.textarea_of(UserSetting.USER_PROMPT.value),
            "General-only instruction",
        )
        _confirm_settings(chat)

        saved = llm_meta(ADMIN_LOGIN)
        if set(saved) != {"general"}:
            raise AssertionError(f"meta.llm keys: {sorted(saved)}")

        chat.open_profile_menu()
        chat.select_profile("search")

        _ask_and_wait(chat)

        payload = _last_request(llm_port)
        if payload.get("temperature") != 0.7:
            raise AssertionError(f"temperature: {payload.get('temperature')!r}")
        if payload.get("model") != "fake-model-search":
            raise AssertionError(f"model: {payload.get('model')!r}")

        prompt = _system_prompt(payload)
        if prompt != SEARCH_PROMPT:
            raise AssertionError(f"system prompt leaked: {prompt!r}")


class TestOtherTabs:
    """Настройки общие на пользователя: другая вкладка того же профиля подхватывает их
    без перезагрузки.
    """

    def test_other_tab_agent_uses_settings_saved_elsewhere(
        self,
        chat: ChatPage,
        open_chat: Any,
        stand: StandProcess,
        llm_port: int,
        llm_meta: LlmMetaReader,
    ) -> None:
        other: ChatPage = open_chat(stand)

        _open_settings(chat)
        _open_tab(chat, PanelTab.MODEL)
        chat.page.locator(PanelSelector.trigger_of(UserSetting.MODEL.value)).click()
        chat.page.get_by_role("option", name="fake-model-alt", exact=True).click()
        _confirm_settings(chat)
        assert llm_meta(ADMIN_LOGIN).get("general") == {"model": "fake-model-alt"}

        # вторая вкладка пересобрала агента по сообщению шины: её запрос идёт с новой моделью
        other.page.wait_for_timeout(1000)
        _ask_and_wait(other)
        assert _last_request(llm_port)["model"] == "fake-model-alt"

        _open_settings(other)
        _open_tab(other, PanelTab.MODEL)
        expect(
            other.page.locator(PanelSelector.trigger_of(UserSetting.MODEL.value))
        ).to_contain_text("fake-model-alt")
