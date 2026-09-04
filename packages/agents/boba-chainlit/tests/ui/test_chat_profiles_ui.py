"""Профили чата в браузере: селектор, применение настроек, набор инструментов.

Проверяется DOM до и после клика по селектору, доставка system prompt / model /
параметров сэмплинга в запрос провайдеру и пересечение инструментов профиля
с ролью пользователя. Ожидаемые значения зафиксированы в конфиге стенда
(StandConfig._use_test_profiles).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx
import pytest
from chat_ui import OpenChat

from boba.stand.ui.chat_page import ChatPage, StepKind
from boba.stand.ui.fake_llm import ScenarioName
from boba.stand.ui.stand import StandProcess, StandUrl

pytestmark = pytest.mark.ui

ADMIN_LOGIN = "admin"
"""Логин с ролью ADM: видит все инструменты, tools=['*']."""

STAND_TOOLS = frozenset(
    {
        "send_file",
        "diagram_save",
        "canvas_open",
        "stream_logs_usage",
        "stream_logs_cleanup",
        "catalog_read",
        "catalog_draft",
        "catalog_propose",
        "catalog_diff",
        "catalog_open",
        "catalog_sync",
        "pipeline_catalog",
        "pipeline_run",
        "connection_list",
    }
)
"""Инструменты, собранные стендом: песочные секции выключены StandConfig,
встроенные (конвейер, каталог соединений) остаются."""

DEV_ROLE_TOOLS = frozenset(
    {"diagram_save", "send_file", "stream_logs_usage", "stream_logs_cleanup"}
)
"""Набор роли DEV в конфиге стенда: без canvas_open."""

SEARCH_PROFILE_TOOLS = frozenset({"diagram_save", "canvas_open"})
"""Набор профиля search в конфиге стенда."""


@pytest.fixture(autouse=True)
def _no_user_overrides(clean_llm_settings: None) -> None:
    """Профили проверяются без личных настроек: их сохраняют соседние тесты."""


@dataclass(frozen=True)
class ProfileSpec:
    """Ожидания одного профиля стенда: что обязано дойти до провайдера."""

    name: str
    display_name: str
    model: str
    system_prompt: str
    temperature: float
    max_tokens: int
    """Уходит провайдеру как max_completion_tokens: так его шлёт langchain-openai."""

    top_p: float | None
    admin_tools: frozenset[str]


PROFILES = (
    ProfileSpec(
        name="general",
        display_name="General",
        model="fake-model-general",
        system_prompt="You are the general stand assistant",
        temperature=0.1,
        max_tokens=1111,
        top_p=0.9,
        admin_tools=STAND_TOOLS,
    ),
    ProfileSpec(
        name="search",
        display_name="Search",
        model="fake-model-search",
        system_prompt="You are the search stand assistant",
        temperature=0.7,
        max_tokens=2222,
        top_p=None,
        admin_tools=SEARCH_PROFILE_TOOLS,
    ),
)


def _llm_requests(llm_port: int) -> list[dict[str, Any]]:
    """Запросы, дошедшие до фейкового провайдера, в порядке прихода."""
    response = httpx.get(StandUrl.of(llm_port, "/requests"), timeout=5.0)
    response.raise_for_status()
    return response.json()["requests"]


def _last_request(llm_port: int) -> dict[str, Any]:
    requests = _llm_requests(llm_port)
    if not requests:
        raise AssertionError("fake llm got no requests")

    return requests[-1]


def _tool_names(payload: dict[str, Any]) -> frozenset[str]:
    """Имена инструментов из запроса провайдеру: их видит LLM."""
    names: set[str] = set()
    for tool in payload.get("tools", []):
        function = tool.get("function", {})
        if name := function.get("name"):
            names.add(str(name))

    return frozenset(names)


def _system_prompt(payload: dict[str, Any]) -> str:
    messages = payload.get("messages", [])
    if not messages:
        raise AssertionError("request has no messages")

    first = messages[0]
    if first.get("role") != "system":
        raise AssertionError(f"first message is not system: {first}")

    return str(first.get("content", ""))


def _ask_and_wait(page: ChatPage) -> None:
    page.ask(f"{ScenarioName.ANSWER.value} please")
    page.await_step(StepKind.ASSISTANT.value)
    page.await_idle()


def _switch_to(page: ChatPage, name: str) -> None:
    page.open_profile_menu()
    page.select_profile(name)


class TestProfileSelectorDom:
    """DOM селектора: до клика, после клика, после выбора."""

    def test_selector_shows_default_profile(self, chat: ChatPage) -> None:
        if not chat.has_profile_selector():
            raise AssertionError(f"no profile selector\n{chat.dom()[:2000]}")

        if chat.profile_label() != "General":
            raise AssertionError(f"default is not General: {chat.profile_label()!r}")

        if chat.profile_menu_open():
            raise AssertionError("profile menu is open before any click")

        if chat.profile_menu_items() != 0:
            raise AssertionError("profile items are in DOM before any click")

    def test_click_reveals_every_profile(self, chat: ChatPage) -> None:
        before = chat.profile_menu_items()

        names = chat.open_profile_menu()

        after = chat.profile_menu_items()
        if before != 0 or after != len(PROFILES):
            raise AssertionError(f"menu items: before={before}, after={after}")

        if not chat.profile_menu_open():
            raise AssertionError("menu is not open after the click")

        if names != [spec.name for spec in PROFILES]:
            raise AssertionError(f"unexpected profiles in menu: {names}")

        chat.close_profile_menu()
        if chat.profile_menu_open():
            raise AssertionError("profile menu did not close")

    @pytest.mark.parametrize("spec", PROFILES, ids=lambda spec: spec.name)
    def test_each_profile_is_clickable(self, chat: ChatPage, spec: ProfileSpec) -> None:
        # клик по уже выбранному пункту не меняет значение: уходим на другой
        if chat.profile_label() == spec.display_name:
            _switch_to(chat, self._other_than(spec).name)

        before = chat.profile_label()
        if before == spec.display_name:
            raise AssertionError(f"profile is already selected: {before!r}")

        _switch_to(chat, spec.name)

        if chat.profile_menu_open():
            raise AssertionError("profile menu did not close after selection")

        if chat.profile_label() != spec.display_name:
            raise AssertionError(
                f"selector shows {chat.profile_label()!r}, "
                f"expected {spec.display_name!r}"
            )

    @staticmethod
    def _other_than(spec: ProfileSpec) -> ProfileSpec:
        for other in PROFILES:
            if other.name != spec.name:
                return other

        raise AssertionError("stand has a single profile, the test needs two")


class TestProfileSettingsApplied:
    """Настройки выбранного профиля реально уходят в запрос провайдеру."""

    @pytest.mark.parametrize("spec", PROFILES, ids=lambda spec: spec.name)
    def test_profile_settings_reach_the_llm(
        self, chat: ChatPage, llm_port: int, spec: ProfileSpec
    ) -> None:
        if spec.name != chat.profile_label().lower():
            _switch_to(chat, spec.name)

        _ask_and_wait(chat)

        payload = _last_request(llm_port)

        if payload.get("model") != spec.model:
            raise AssertionError(f"model: {payload.get('model')!r} != {spec.model!r}")

        if _system_prompt(payload) != spec.system_prompt:
            raise AssertionError(f"system prompt: {_system_prompt(payload)!r}")

        if payload.get("temperature") != spec.temperature:
            raise AssertionError(f"temperature: {payload.get('temperature')!r}")

        sent_tokens = payload.get("max_completion_tokens")
        if sent_tokens != spec.max_tokens:
            raise AssertionError(f"max_completion_tokens: {sent_tokens!r}")

        if spec.top_p is None:
            if "top_p" in payload:
                raise AssertionError(f"top_p leaked: {payload.get('top_p')!r}")
        elif payload.get("top_p") != spec.top_p:
            raise AssertionError(f"top_p: {payload.get('top_p')!r}")

    @pytest.mark.parametrize("spec", PROFILES, ids=lambda spec: spec.name)
    def test_admin_tools_follow_the_profile(
        self, chat: ChatPage, llm_port: int, spec: ProfileSpec
    ) -> None:
        if spec.name != chat.profile_label().lower():
            _switch_to(chat, spec.name)

        _ask_and_wait(chat)

        tools = _tool_names(_last_request(llm_port))
        if tools != spec.admin_tools:
            raise AssertionError(f"{spec.name}: tools {sorted(tools)}")


class TestRoleIntersection:
    """Инструменты хода — пересечение набора профиля и набора роли."""

    @staticmethod
    def _dev_login(stand: StandProcess) -> str:
        """Логин с ролью DEV без ADM; без такого пользователя тест не имеет смысла."""
        for login, roles in stand.config.local_users().items():
            if "DEV" in roles and "ADM" not in roles:
                return login

        pytest.skip("no DEV-only login in [auth.local] of the base config")

    def test_dev_search_gets_the_intersection(
        self, open_chat: OpenChat, stand: StandProcess, llm_port: int
    ) -> None:
        page = open_chat(stand, self._dev_login(stand))

        _switch_to(page, "search")
        _ask_and_wait(page)

        tools = _tool_names(_last_request(llm_port))
        expected = SEARCH_PROFILE_TOOLS & DEV_ROLE_TOOLS
        if tools != expected:
            raise AssertionError(f"tools {sorted(tools)} != {sorted(expected)}")

    def test_dev_general_is_cut_by_the_role(
        self, open_chat: OpenChat, stand: StandProcess, llm_port: int
    ) -> None:
        page = open_chat(stand, self._dev_login(stand))

        _ask_and_wait(page)

        tools = _tool_names(_last_request(llm_port))
        if tools != DEV_ROLE_TOOLS:
            raise AssertionError(f"tools {sorted(tools)} != {sorted(DEV_ROLE_TOOLS)}")


class TestSingleProfile:
    """Единственный профиль: селектора нет, профиль применяется автоматически."""

    def test_no_selector_and_auto_applied(
        self, open_chat: OpenChat, solo_stand: StandProcess, llm_port: int
    ) -> None:
        page = open_chat(solo_stand, ADMIN_LOGIN)

        if page.has_profile_selector():
            raise AssertionError(
                f"selector is drawn for a single profile\n{page.dom()[:2000]}"
            )

        _ask_and_wait(page)

        payload = _last_request(llm_port)
        if payload.get("model") != "fake-model-general":
            raise AssertionError(f"model: {payload.get('model')!r}")

        if _system_prompt(payload) != "You are the general stand assistant":
            raise AssertionError(f"system prompt: {_system_prompt(payload)!r}")

        if _tool_names(payload) != STAND_TOOLS:
            raise AssertionError(f"tools: {sorted(_tool_names(payload))}")
