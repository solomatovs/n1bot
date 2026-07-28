import pytest

from boba.hermes.agent.models import HermesMessage, HermesSession
from boba.hermes.chat.data.hermes_codec import (
    HermesHistoryCodec,
    HermesImageCodec,
    HermesMessageCodec,
    HermesSessionCodec,
    HermesTimestamp,
)

USER_ID = "0f9d2f1a-0000-4000-8000-000000000001"
SESSION_ID = "6f1c0e3c-0000-4000-8000-000000000002"


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    """Конвертерам не нужны ни chainlit-контекст, ни postgres.

    Подменяет autouse-фикстуру из conftest, которая поднимает их для тестов
    data layer.
    """


def session(**fields) -> HermesSession:
    return HermesSession.from_api(
        {
            "id": SESSION_ID,
            "source": "api_server",
            "started_at": 1785168444.0,
            **fields,
        }
    )


def message(**fields) -> HermesMessage:
    return HermesMessage.from_api(
        {
            "id": 1,
            "session_id": SESSION_ID,
            "role": "user",
            "timestamp": 1785168444.0,
            **fields,
        }
    )


def test_session_becomes_thread():
    thread = HermesSessionCodec().to_thread(
        session(title="разбор логов", model="hermes-agent", message_count=4),
        user_id=USER_ID,
        user_identifier="solomatovs",
        steps=[],
        elements=[],
    )

    assert thread["id"] == SESSION_ID
    assert thread["name"] == "разбор логов"
    assert thread["userId"] == USER_ID
    assert thread["userIdentifier"] == "solomatovs"
    assert thread["createdAt"].startswith("2026-")
    assert (thread["metadata"] or {})["message_count"] == 4


def test_thread_name_falls_back_to_preview():
    thread = HermesSessionCodec().to_thread(
        session(title="  ", preview="почему упал gateway"),
        user_id=None,
        user_identifier=None,
        steps=[],
        elements=[],
    )

    # сессия без заголовка не должна выглядеть в сайдбаре безымянной
    assert thread["name"] == "почему упал gateway"


def test_thread_name_is_none_without_title_and_preview():
    thread = HermesSessionCodec().to_thread(
        session(), user_id=None, user_identifier=None, steps=[], elements=[]
    )

    assert thread["name"] is None


def test_thread_metadata_skips_empty_counters():
    thread = HermesSessionCodec().to_thread(
        session(model="hermes-agent", estimated_cost_usd=None),
        user_id=None,
        user_identifier=None,
        steps=[],
        elements=[],
    )

    assert thread["metadata"] == {"model": "hermes-agent", "source": "api_server"}


def test_user_message_becomes_step():
    step = HermesMessageCodec().to_step(
        SESSION_ID,
        message(
            id=1,
            session_id=SESSION_ID,
            role="user",
            content="привет",
            timestamp=1785168444.0,
        ),
    )

    assert step is not None
    assert step.get("type") == "user_message"
    assert step.get("output") == "привет"
    assert step.get("threadId") == SESSION_ID


def test_step_always_carries_thread_id():
    # по threadId фронт связывает шаг с тредом при оценках и вложениях,
    # поэтому он берётся от вызывающего, а не из ответа hermes
    step = HermesMessageCodec().to_step(
        SESSION_ID, message(id=1, role="user", content="привет")
    )

    assert step is not None
    assert step.get("threadId") == SESSION_ID


def test_assistant_reasoning_goes_to_metadata():
    step = HermesMessageCodec().to_step(
        SESSION_ID,
        message(
            id=2,
            role="assistant",
            content="готово",
            reasoning="сначала проверил логи",
            finish_reason="stop",
        )
    )

    assert step is not None
    # рассуждение не часть ответа: в тексте шага его быть не должно
    assert step.get("output") == "готово"
    assert (step.get("metadata") or {})["reasoning"] == "сначала проверил логи"
    assert (step.get("metadata") or {})["finish_reason"] == "stop"


def test_tool_step_takes_tool_name():
    step = HermesMessageCodec().to_step(
        SESSION_ID,
        message(id=3, role="tool", tool_name="shell", content="ok")
    )

    assert step is not None
    assert step.get("type") == "tool"
    assert step.get("name") == "shell"


@pytest.mark.parametrize("role", ["system", "developer", "unknown", ""])
def test_service_roles_are_skipped(role: str):
    # системный промпт — снимок конфигурации, в диалоге его показывать нечего
    assert HermesMessageCodec().to_step(SESSION_ID, message(id=4, role=role)) is None


def test_tool_result_nests_into_calling_assistant_step():
    messages = [
        message(id=1, role="user", content="посмотри логи"),
        message(
            id=2,
            role="assistant",
            tool_calls=[{"id": "call-1", "function": {"name": "shell"}}],
        ),
        message(
            id=3,
            role="tool",
            tool_call_id="call-1",
            tool_name="shell",
            content="ничего подозрительного",
        ),
    ]

    steps = HermesMessageCodec().to_steps(SESSION_ID, messages)

    # иначе результат инструмента встанет в диалог отдельным сообщением
    assert [s.get("id") for s in steps] == ["1", "2", "3"]
    assert steps[2].get("parentId") == "2"
    assert "parentId" not in steps[0]


def test_orphan_tool_result_has_no_parent():
    orphan = message(id=1, role="tool", tool_call_id="потерянный", content="x")

    steps = HermesMessageCodec().to_steps(SESSION_ID, [orphan])

    # вызов мог уехать за границу компрессии истории — шаг всё равно нужен
    assert len(steps) == 1
    assert "parentId" not in steps[0]


def test_steps_keep_hermes_order():
    messages = [message(id=i, role="user", content=str(i)) for i in range(5)]

    steps = HermesMessageCodec().to_steps(SESSION_ID, messages)

    assert [s.get("id") for s in steps] == ["0", "1", "2", "3", "4"]


def test_timestamp_converts_unix_seconds():
    assert HermesTimestamp.iso(1785168444.0).startswith("2026-")


def test_timestamp_keeps_ready_string():
    ready = "2026-07-27T12:00:00+00:00"

    assert HermesTimestamp.iso(ready) == ready


def test_timestamp_substitutes_now_when_missing():
    # chainlit сортирует треды по createdAt: пустое значение ломает сайдбар
    assert HermesTimestamp.iso(None).startswith("20")


@pytest.mark.parametrize("part_type", ["text", "input_text", "output_text"])
def test_text_parts_of_any_kind_are_taken(part_type: str):
    # api_server нормализует входящие в "text", но историю пишет не только он
    step = HermesMessageCodec().to_step(
        SESSION_ID,
        message(role="user", content=[{"type": part_type, "text": "что на картинке"}]),
    )

    assert step is not None
    assert step.get("output") == "что на картинке"


def test_image_parts_are_skipped():
    content = [
        {"type": "text", "text": "что на картинке"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,iVBOR"}},
        {"type": "text", "text": "и почему"},
    ]

    step = HermesMessageCodec().to_step(
        SESSION_ID, message(role="user", content=content)
    )

    assert step is not None
    # base64 картинки в тексте шага не место
    assert step.get("output") == "что на картинке\nи почему"


def test_bare_string_part_is_taken():
    step = HermesMessageCodec().to_step(
        SESSION_ID, message(role="user", content=["просто строка"])
    )

    assert step is not None
    assert step.get("output") == "просто строка"


def test_content_without_text_gives_empty_step():
    content = [{"type": "image_url", "image_url": {"url": "https://example/x.png"}}]

    step = HermesMessageCodec().to_step(
        SESSION_ID, message(role="user", content=content)
    )

    assert step is not None
    assert step.get("output") == ""


def test_image_becomes_element_attached_to_step():
    content = [
        {"type": "text", "text": "что на картинке"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,iVBOR"}},
    ]
    msg = message(id=7, role="user", content=content)

    step = HermesMessageCodec().to_step(SESSION_ID, msg)
    elements = HermesImageCodec().of_message(SESSION_ID, msg)

    assert step is not None
    # картинка не теряется: в тексте её нет, но она приходит элементом
    assert step.get("output") == "что на картинке"
    assert len(elements) == 1
    assert elements[0].get("forId") == step.get("id")
    assert elements[0].get("threadId") == SESSION_ID
    assert elements[0].get("type") == "image"
    assert elements[0].get("url") == "data:image/png;base64,iVBOR"


def test_input_image_without_nested_object():
    # источники помимо api_server кладут ссылку прямо в поле
    msg = message(role="user", content=[{"type": "input_image", "url": "https://x/a.png"}])

    elements = HermesImageCodec().of_message(SESSION_ID, msg)

    assert [e.get("url") for e in elements] == ["https://x/a.png"]


def test_several_images_get_distinct_ids():
    content = [
        {"type": "image_url", "image_url": {"url": "https://x/1.png"}},
        {"type": "image_url", "image_url": {"url": "https://x/2.png"}},
    ]

    elements = HermesImageCodec().of_message(SESSION_ID, message(id=3, content=content))

    assert [e.get("id") for e in elements] == ["3-image-0", "3-image-1"]


def test_text_only_message_has_no_elements():
    text_only = message(content="просто текст")

    assert HermesImageCodec().of_message(SESSION_ID, text_only) == []


def test_elements_collected_over_history():
    messages = [
        message(id=1, role="user", content=[{"type": "image_url", "image_url": {"url": "https://x/1.png"}}]),
        message(id=2, role="assistant", content="вижу"),
        message(id=3, role="user", content=[{"type": "image_url", "image_url": {"url": "https://x/2.png"}}]),
    ]

    elements = HermesImageCodec().to_elements(SESSION_ID, messages)

    assert [e.get("forId") for e in elements] == ["1", "3"]


def test_history_gives_steps_and_elements_together():
    messages = [
        message(
            id=1,
            role="user",
            content=[
                {"type": "text", "text": "что на картинке"},
                {"type": "image_url", "image_url": {"url": "https://x/1.png"}},
            ],
        ),
        message(id=2, role="assistant", content="кот"),
    ]

    history = HermesHistoryCodec().of(SESSION_ID, messages)

    # один вход на разбор истории: элементы нельзя потерять, забыв вызвать
    # второй конвертер
    assert [s.get("id") for s in history.steps] == ["1", "2"]
    assert [e.get("forId") for e in history.elements] == ["1"]
    assert history.elements[0].get("url") == "https://x/1.png"


def test_history_without_images_has_no_elements():
    history = HermesHistoryCodec().of(SESSION_ID, [message(content="просто текст")])

    assert len(history.steps) == 1
    assert history.elements == []
