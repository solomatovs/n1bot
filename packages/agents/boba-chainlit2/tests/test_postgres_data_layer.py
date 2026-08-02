"""
тесты PostgresDataLayer
"""

from pathlib import Path
from uuid import uuid4

import pytest
from chainlit.element import Text
from chainlit.step import StepDict
from chainlit.types import Feedback as FeedbackPayload
from chainlit.types import Pagination, ThreadFilter
from chainlit.user import User as ChainlitUser
from conftest import Seed

from boba.chainlit2.chat.data import data_layer as data_layer_module
from boba.chainlit2.chat.data.data_layer import PostgresDataLayer
from boba.chainlit2.chat.data.models import Element

pytestmark = pytest.mark.anyio


async def test_setup_is_idempotent(layer: PostgresDataLayer):
    await layer.setup()


async def test_create_and_get_user(layer: PostgresDataLayer):
    created = await layer.create_user(
        ChainlitUser(identifier="alice", metadata={"k": "v"})
    )
    assert created is not None
    assert created.identifier == "alice"

    fetched = await layer.get_user("alice")
    assert fetched is not None
    assert fetched.id == created.id
    assert fetched.identifier == "alice"

    assert await layer.get_user("does-not-exist") is None


async def test_update_thread_and_author(seeded: Seed):
    layer = seeded.layer
    author = await layer.get_thread_author(seeded.thread_id)
    assert author == seeded.user.identifier

    await layer.update_thread(seeded.thread_id, name="renamed")
    thread = await layer.get_thread(seeded.thread_id)
    assert thread is not None
    assert thread["name"] == "renamed"
    assert thread["userId"] == seeded.user.id


async def test_steps_are_not_persisted(seeded: Seed):
    layer = seeded.layer
    step: StepDict = {
        "id": str(uuid4()),
        "threadId": seeded.thread_id,
        "type": "assistant_message",
        "name": "assistant",
        "output": "should not be stored",
    }
    await layer.create_step(step)
    await layer.update_step(step)

    thread = await layer.get_thread(seeded.thread_id)
    assert thread is not None
    assert all(s.get("id") != step["id"] for s in thread["steps"])
    assert await layer.get_favorite_steps(seeded.user.id) == []


async def test_upsert_and_delete_feedback(seeded: Seed):
    layer = seeded.layer
    feedback = FeedbackPayload(
        forId=seeded.answer_step_id,
        value=1,
        comment="nice",
        threadId=seeded.thread_id,
    )
    feedback_id = await layer.upsert_feedback(feedback)
    assert feedback_id

    thread = await layer.get_thread(seeded.thread_id)
    assert thread is not None
    step = next(s for s in thread["steps"] if s.get("id") == seeded.answer_step_id)
    feedback_dict = step.get("feedback")
    assert feedback_dict is not None
    assert feedback_dict["value"] == 1

    assert await layer.delete_feedback(feedback_id) is True


async def test_create_get_delete_element(
    seeded: Seed, files_dir: Path, monkeypatch: pytest.MonkeyPatch
):
    layer = seeded.layer
    monkeypatch.setattr(data_layer_module, "current_user_id", lambda: seeded.user.id)

    element = Text(
        thread_id=seeded.thread_id,
        for_id=seeded.answer_step_id,
        name="note.txt",
        content="payload",
    )
    await layer.create_element(element)

    fetched = await layer.get_element(seeded.thread_id, element.id)
    assert fetched is not None
    assert fetched.get("id") == element.id
    assert fetched.get("url")

    # путь не хранится, а вычисляется по шаблону от пользователя сессии
    object_key = Element.object_key(
        seeded.user.id, seeded.thread_id, element.name, element.id
    )
    assert object_key.endswith("/upload/note.txt")
    assert (files_dir / object_key).read_bytes() == b"payload"

    await layer.delete_element(element.id)
    assert await layer.get_element(seeded.thread_id, element.id) is None
    assert not (files_dir / object_key).exists()


async def test_get_thread_builds_steps_from_history(seeded: Seed):
    layer = seeded.layer
    thread = await layer.get_thread(seeded.thread_id)
    assert thread is not None
    assert thread["id"] == seeded.thread_id
    assert thread["tags"] == ["a"]

    outputs = [(s.get("type"), s.get("output")) for s in thread["steps"]]
    assert outputs == [("user_message", "hi"), ("assistant_message", "hello")]
    answer = thread["steps"][1]
    assert answer.get("id") == seeded.answer_step_id
    assert answer.get("feedback") is None
    assert thread["elements"] == []

    assert await layer.get_thread(str(uuid4())) is None


async def test_thread_without_history_has_no_steps(seeded: Seed):
    layer = seeded.layer
    await layer.update_thread(str(uuid4()), user_id=seeded.user.id)
    threads = await layer.list_threads(
        Pagination(first=10), ThreadFilter(userId=seeded.user.id)
    )
    empty = next(t for t in threads.data if t["id"] != seeded.thread_id)
    thread = await layer.get_thread(empty["id"])
    assert thread is not None
    assert thread["steps"] == []


async def test_list_threads(seeded: Seed):
    layer = seeded.layer
    page = await layer.list_threads(
        Pagination(first=10),
        ThreadFilter(userId=seeded.user.id),
    )
    assert any(t["id"] == seeded.thread_id for t in page.data)

    with pytest.raises(ValueError, match="userId is required"):
        await layer.list_threads(Pagination(first=10), ThreadFilter())

async def test_delete_thread(seeded: Seed):
    layer = seeded.layer
    await layer.delete_thread(seeded.thread_id)
    assert await layer.get_thread(seeded.thread_id) is None


async def test_build_debug_url(layer: PostgresDataLayer):
    assert await layer.build_debug_url() == ""


async def test_close_releases_storage(layer: PostgresDataLayer):
    await layer.close()
