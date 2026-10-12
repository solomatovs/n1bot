"""Панель настроек агента в браузере: вкладки «Агент» и «Сессия», слой
пользователя в Postgres и запрос к LLM.

Вкладка «Агент» — адрес, ключ и модель провайдера поверх профиля; вкладка
«Сессия» появляется с первой сессией треда и несёт режим разрешений и профиль
модели. Confirm сохраняет правку в слой пользователя и в слой открытого чата и
перезапускает процесс агента, поэтому запрос к LLM сверяется и в том же чате,
и в новом. Ожидаемые значения профилей заданы стендом
(StandConfig._use_test_profiles).
"""

from __future__ import annotations

import tomllib
from enum import StrEnum
from pathlib import Path as FilePath
from typing import Any

import httpx
import pytest
from chat_ui import UserLayerReader
from playwright.sync_api import expect

from boba.chainlit.agent.panel import AgentPanelTab
from boba.chainlit.chat.panel_text import PanelText
from boba.chat.profiles import UserSetting
from boba.stand.ui.chat_page import ChatPage, StepKind
from boba.stand.ui.fake_llm import ScenarioName
from boba.stand.ui.stand import StandProcess, StandUrl

pytestmark = pytest.mark.ui

ADMIN_LOGIN = "admin"
PROFILE = "general"
GENERAL_PROMPT = "You are the general stand assistant"
SEARCH_PROMPT = "You are the search stand assistant"
SESSION_MODE = "session_mode"
SESSION_MODEL = "session_model"
BYPASS_MODE = "Bypass permissions"
DEFAULT_MODE = "Default"
KEPT_HINT = "(kept)"
ENCRYPTED_PREFIX = "enc:v1:"


def _app_language() -> str:
    """Язык интерфейса: его навязывает секция [UI] конфига chainlit.

    Подписи панели тест ищет на этом языке; иначе он ждёт английские названия
    вкладок, а панель нарисована на языке развёртывания.
    """
    assets = FilePath(__file__).resolve().parents[2] / "assets"
    config = assets / ".chainlit" / "config.toml"
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
"""Пауза после Confirm: сохранение идёт по сокету."""


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
    def trigger_of(field: str) -> str:
        return f"button#{field}"


@pytest.fixture(autouse=True)
def _fresh_settings(clean_agent_settings: None) -> None:
    """Каждый тест начинает без сохранённых настроек прошлых тестов."""


def _last_request(llm_port: int) -> dict[str, Any]:
    response = httpx.get(StandUrl.of(llm_port, "/requests"), timeout=5.0)
    response.raise_for_status()
    requests = response.json()["requests"]
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


def _open_tab(page: ChatPage, tab: AgentPanelTab) -> None:
    page.page.get_by_role("tab", name=TEXT.tab(tab.value), exact=True).click()
    page.page.wait_for_timeout(300)


def _tab_labels(page: ChatPage) -> set[str]:
    labels: set[str] = set()
    tabs = page.page.locator(PanelSelector.TAB.value)
    for index in range(tabs.count()):
        labels.add(tabs.nth(index).inner_text().strip())

    return labels


def _confirm_settings(page: ChatPage) -> None:
    page.page.locator(PanelSelector.CONFIRM.value).click()
    page.page.wait_for_timeout(SAVE_GRACE_MS)


def _fill(page: ChatPage, setting: UserSetting, value: str) -> None:
    page.page.fill(PanelSelector.input_of(setting.value), value)


def _value(page: ChatPage, setting: UserSetting) -> str:
    return page.page.locator(PanelSelector.input_of(setting.value)).input_value()


def _save_agent_field(page: ChatPage, setting: UserSetting, value: str) -> None:
    """Вкладка «Агент»: значение в поле и Confirm."""
    _open_settings(page)
    _open_tab(page, AgentPanelTab.AGENT)
    _fill(page, setting, value)
    _confirm_settings(page)


def _ask_and_wait(page: ChatPage) -> None:
    page.ask(f"{ScenarioName.ANSWER.value} please")
    page.await_step(StepKind.ASSISTANT.value)
    page.await_idle()


def _new_chat(page: ChatPage) -> None:
    """Новый чат во вкладке: его слой настроек копируется из слоя пользователя."""
    page.log.clear()
    page.open()


class TestAgentFields:
    """Поля вкладки «Агент»: ввод, слой пользователя и запрос к LLM."""

    def test_model_applies_to_the_open_chat_and_to_new_chats(
        self, chat: ChatPage, llm_port: int, user_layer: UserLayerReader
    ) -> None:
        _open_settings(chat)
        _open_tab(chat, AgentPanelTab.AGENT)
        if _value(chat, UserSetting.AGENT_MODEL) != "":
            raise AssertionError("model field is not empty before the edit")

        _fill(chat, UserSetting.AGENT_MODEL, "fake-model-picked")
        _confirm_settings(chat)

        stored = user_layer(ADMIN_LOGIN, PROFILE)
        if stored != {"model": "fake-model-picked"}:
            raise AssertionError(f"user layer: {stored}")

        # открытый пустой чат: агент перезапущен из нового слоя чата
        _ask_and_wait(chat)
        payload = _last_request(llm_port)
        if payload.get("model") != "fake-model-picked":
            raise AssertionError(f"model of the open chat: {payload.get('model')!r}")
        if _system_prompt(payload) != GENERAL_PROMPT:
            raise AssertionError(f"system prompt: {_system_prompt(payload)!r}")

        _new_chat(chat)
        _ask_and_wait(chat)
        payload = _last_request(llm_port)
        if payload.get("model") != "fake-model-picked":
            raise AssertionError(f"model of a new chat: {payload.get('model')!r}")

    def test_provider_url_is_stored_and_used(
        self, chat: ChatPage, llm_port: int, user_layer: UserLayerReader
    ) -> None:
        url = StandUrl.of(llm_port, "/v1/")
        _save_agent_field(chat, UserSetting.AGENT_BASE_URL, url)

        stored = user_layer(ADMIN_LOGIN, PROFILE)
        if stored != {"base_url": url}:
            raise AssertionError(f"user layer: {stored}")

        _ask_and_wait(chat)
        if _last_request(llm_port).get("model") != "fake-model-general":
            raise AssertionError("the turn did not reach the model by the saved url")

    def test_api_key_is_encrypted_and_never_shown(
        self, chat: ChatPage, user_layer: UserLayerReader
    ) -> None:
        _save_agent_field(chat, UserSetting.AGENT_API_KEY, "sk-ui-secret")

        stored = user_layer(ADMIN_LOGIN, PROFILE)
        if set(stored) != {"api_key"}:
            raise AssertionError(f"user layer keys: {sorted(stored)}")
        if not str(stored["api_key"]).startswith(ENCRYPTED_PREFIX):
            raise AssertionError("api key is stored without encryption")

        _open_settings(chat)
        _open_tab(chat, AgentPanelTab.AGENT)
        field = chat.page.locator(
            PanelSelector.input_of(UserSetting.AGENT_API_KEY.value)
        )
        if field.input_value() != "":
            raise AssertionError("the saved api key is shown in the panel")
        if field.get_attribute("placeholder") != KEPT_HINT:
            raise AssertionError(f"placeholder: {field.get_attribute('placeholder')!r}")

        # пустое поле оставляет сохранённый ключ
        _confirm_settings(chat)
        if user_layer(ADMIN_LOGIN, PROFILE) != stored:
            raise AssertionError("an empty key field replaced the saved key")


class TestProfileValueBack:
    """Нетронутая панель слой пользователя не заводит."""

    def test_untouched_panel_stores_nothing(
        self, chat: ChatPage, llm_port: int, user_layer: UserLayerReader
    ) -> None:
        _open_settings(chat)
        _confirm_settings(chat)

        if user_layer(ADMIN_LOGIN, PROFILE):
            raise AssertionError(
                f"stored without edits: {user_layer(ADMIN_LOGIN, PROFILE)}"
            )

        _ask_and_wait(chat)
        payload = _last_request(llm_port)

        # сэмплинг задаёт только админская таблица профиля: незаданные ключи
        # в запрос не попадают, заданные уходят как написаны
        for absent in ("frequency_penalty", "presence_penalty", "seed"):
            if absent in payload:
                raise AssertionError(f"{absent} leaked: {payload.get(absent)!r}")

        if payload.get("temperature") != 0.1:
            raise AssertionError(f"temperature: {payload.get('temperature')!r}")
        if payload.get("top_p") != 0.9:
            raise AssertionError(f"top_p: {payload.get('top_p')!r}")
        if payload.get("model") != "fake-model-general":
            raise AssertionError(f"model: {payload.get('model')!r}")
        if _system_prompt(payload) != GENERAL_PROMPT:
            raise AssertionError(f"system prompt: {_system_prompt(payload)!r}")


class TestWidgetDescriptions:
    """У каждой настройки в панели есть пояснение."""

    def test_every_widget_shows_a_description(self, chat: ChatPage) -> None:
        _open_settings(chat)
        _open_tab(chat, AgentPanelTab.AGENT)

        text = chat.page.locator(PanelSelector.MODAL.value).inner_text()
        for setting in UserSetting:
            hint = TEXT.field_description(setting.value).split(";")[0]
            if hint[:40] not in text:
                raise AssertionError(f"{setting.value}: description is not shown")


class TestAllowedSettingsVisibility:
    """Панель показывает только вкладки, разрешённые профилем."""

    def test_general_shows_the_agent_tab_before_the_first_turn(
        self, chat: ChatPage
    ) -> None:
        _open_settings(chat)

        # вкладка сессии появляется после первого хода: до него настроек сессии нет
        labels = _tab_labels(chat)
        if labels != {TEXT.tab(AgentPanelTab.AGENT.value)}:
            raise AssertionError(f"tabs: {sorted(labels)}")

        for setting in UserSetting:
            selector = PanelSelector.input_of(setting.value)
            if not chat.page.locator(selector).count():
                raise AssertionError(f"widget {setting.value} is not drawn")

    def test_search_hides_the_agent_tab(self, chat: ChatPage) -> None:
        chat.open_profile_menu()
        chat.select_profile("search")
        chat.page.wait_for_timeout(1000)

        # search слой пользователя не открывает: до первого хода панели нет
        if chat.page.locator(PanelSelector.OPEN_MODAL.value).count():
            raise AssertionError("settings button is drawn for a closed profile")

        _ask_and_wait(chat)
        chat.page.wait_for_selector(PanelSelector.OPEN_MODAL.value, timeout=15000)
        _open_settings(chat)
        labels = _tab_labels(chat)
        if labels != {TEXT.tab(AgentPanelTab.SESSION.value)}:
            raise AssertionError(f"tabs: {sorted(labels)}")


class TestSettingsLifecycle:
    def test_settings_survive_reload(self, chat: ChatPage) -> None:
        _save_agent_field(chat, UserSetting.AGENT_MODEL, "fake-model-kept")

        chat.page.reload(wait_until="domcontentloaded")
        chat.page.wait_for_selector("#chat-input", timeout=60000)
        chat.page.wait_for_timeout(1000)

        _open_settings(chat)
        _open_tab(chat, AgentPanelTab.AGENT)
        value = _value(chat, UserSetting.AGENT_MODEL)
        if value != "fake-model-kept":
            raise AssertionError(f"agent_model after reload: {value!r}")


class TestSessionTab:
    """Вкладка «Сессия»: появляется с первой сессией треда, её выбор пишется в
    слой чата и применяется перезапуском агента."""

    def test_session_tab_appears_after_the_first_message(self, chat: ChatPage) -> None:
        _ask_and_wait(chat)

        _open_settings(chat)
        labels = _tab_labels(chat)
        expected = {
            TEXT.tab(AgentPanelTab.AGENT.value),
            TEXT.tab(AgentPanelTab.SESSION.value),
        }
        if labels != expected:
            raise AssertionError(f"tabs: {sorted(labels)}")

        _open_tab(chat, AgentPanelTab.SESSION)
        for field in (SESSION_MODE, SESSION_MODEL):
            if not chat.page.locator(PanelSelector.trigger_of(field)).count():
                raise AssertionError(f"widget {field} is not drawn")

        mode = chat.page.locator(PanelSelector.trigger_of(SESSION_MODE))
        if BYPASS_MODE not in mode.inner_text():
            raise AssertionError(f"session mode: {mode.inner_text()!r}")

    def test_panel_survives_thread_resume(self, chat: ChatPage) -> None:
        """Обновление страницы треда идёт resume'ом: шестерёнка обязана вернуться."""
        _ask_and_wait(chat)

        chat.page.reload(wait_until="domcontentloaded")
        chat.page.wait_for_selector("#chat-input", timeout=60000)
        chat.page.wait_for_selector(PanelSelector.OPEN_MODAL.value, timeout=15000)

        # сессию ещё держит процесс прежнего подключения вкладки: новый процесс —
        # наблюдатель, вариантов выбора сессии у него нет, вкладка «Агент» на месте
        _open_settings(chat)
        _open_tab(chat, AgentPanelTab.AGENT)
        field = PanelSelector.input_of(UserSetting.AGENT_MODEL.value)
        if not chat.page.locator(field).count():
            raise AssertionError("после resume панель открылась без виджетов")

    def test_changed_mode_is_shown_after_the_restart_and_the_chat_goes_on(
        self, chat: ChatPage, llm_port: int
    ) -> None:
        _ask_and_wait(chat)
        answers = chat.steps_of(StepKind.ASSISTANT.value).count()

        _open_settings(chat)
        _open_tab(chat, AgentPanelTab.SESSION)
        chat.page.locator(PanelSelector.trigger_of(SESSION_MODE)).click()
        chat.page.get_by_role("option", name=DEFAULT_MODE, exact=True).click()
        _confirm_settings(chat)

        # перезапуск ленту не воспроизводит: ответов столько же
        chat.page.wait_for_timeout(3000)
        if chat.steps_of(StepKind.ASSISTANT.value).count() != answers:
            raise AssertionError("the feed was replayed by the restart")

        _open_settings(chat)
        _open_tab(chat, AgentPanelTab.SESSION)
        mode = chat.page.locator(PanelSelector.trigger_of(SESSION_MODE))
        expect(mode).to_contain_text(DEFAULT_MODE)
        chat.page.keyboard.press("Escape")

        _ask_and_wait(chat)
        payload = _last_request(llm_port)
        asked = [m for m in payload.get("messages", []) if m.get("role") == "user"]
        if len(asked) != 2:
            raise AssertionError(f"the session lost its history: {asked}")


class TestSettingsIsolation:
    """Настройки general не текут в search: у каждого профиля свои."""

    def test_other_profile_keeps_its_own_settings(
        self, chat: ChatPage, llm_port: int, user_layer: UserLayerReader
    ) -> None:
        _save_agent_field(chat, UserSetting.AGENT_MODEL, "fake-model-general-only")

        if user_layer(ADMIN_LOGIN, "search"):
            raise AssertionError(f"search layer: {user_layer(ADMIN_LOGIN, 'search')}")

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
    """Слой пользователя общий на пользователя: другая вкладка того же профиля
    видит его в панели без перезагрузки, а её новый чат идёт уже с ним.
    """

    def test_other_tab_agent_uses_settings_saved_elsewhere(
        self,
        chat: ChatPage,
        open_chat: Any,
        stand: StandProcess,
        llm_port: int,
        user_layer: UserLayerReader,
    ) -> None:
        other: ChatPage = open_chat(stand)

        _save_agent_field(chat, UserSetting.AGENT_MODEL, "fake-model-shared")
        assert user_layer(ADMIN_LOGIN, PROFILE) == {"model": "fake-model-shared"}

        # панель второй вкладки обновилась по сообщению шины
        _open_settings(other)
        _open_tab(other, AgentPanelTab.AGENT)
        expect(
            other.page.locator(PanelSelector.input_of(UserSetting.AGENT_MODEL.value))
        ).to_have_value("fake-model-shared")
        other.page.keyboard.press("Escape")

        # новый чат второй вкладки берёт слой пользователя
        _new_chat(other)
        _ask_and_wait(other)
        payload = _last_request(llm_port)
        assert payload.get("model") == "fake-model-shared"
        assert _system_prompt(payload) == GENERAL_PROMPT
