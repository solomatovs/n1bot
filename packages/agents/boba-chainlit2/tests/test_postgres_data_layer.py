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

from boba.chainlit2.chat.data.data_layer import PostgresDataLayer

pytestmark = pytest.mark.anyio


async def test_setup_is_idempotent(layer: PostgresDataLayer):
    """setup() можно звать повторно без ошибок (CREATE ... IF NOT EXISTS)."""
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

    # повторный update мержит поля, не затирая существующие
    await layer.update_thread(seeded.thread_id, name="renamed")
    thread = await layer.get_thread(seeded.thread_id)
    assert thread is not None
    assert thread["name"] == "renamed"
    assert thread["userId"] == seeded.user.id


async def test_create_update_step_and_favorites(seeded: Seed):
    layer = seeded.layer

    # обновляем существующий шаг (upsert по id)
    updated: StepDict = {**seeded.step, "output": "hello-2"}
    await layer.update_step(updated)

    favorites = await layer.get_favorite_steps(seeded.user.id)
    ids = {s.get("id") for s in favorites}
    assert seeded.step_id in ids
    fav = next(s for s in favorites if s.get("id") == seeded.step_id)
    assert fav.get("output") == "hello-2"


async def test_delete_step(seeded: Seed):
    layer = seeded.layer
    await layer.delete_step(seeded.step_id)

    thread = await layer.get_thread(seeded.thread_id)
    assert thread is not None
    assert all(s.get("id") != seeded.step_id for s in thread["steps"])


async def test_upsert_and_delete_feedback(seeded: Seed):
    layer = seeded.layer
    feedback = FeedbackPayload(
        forId=seeded.step_id,
        value=1,
        comment="nice",
        threadId=seeded.thread_id,
    )
    feedback_id = await layer.upsert_feedback(feedback)
    assert feedback_id

    # feedback долетает до шага в собранном треде
    thread = await layer.get_thread(seeded.thread_id)
    assert thread is not None
    step = next(s for s in thread["steps"] if s.get("id") == seeded.step_id)
    feedback_dict = step.get("feedback")
    assert feedback_dict is not None
    assert feedback_dict["value"] == 1

    assert await layer.delete_feedback(feedback_id) is True


async def test_create_get_delete_element(seeded: Seed, files_dir: Path):
    layer = seeded.layer

    element = Text(
        thread_id=seeded.thread_id,
        for_id=seeded.step_id,
        name="note.txt",
        content="payload",
    )
    await layer.create_element(element)

    fetched = await layer.get_element(seeded.thread_id, element.id)
    assert fetched is not None
    assert fetched.get("id") == element.id
    object_key = fetched.get("objectKey")
    assert object_key is not None
    # файл реально лёг на диск под object_key
    assert (files_dir / object_key).read_bytes() == b"payload"

    await layer.delete_element(element.id)
    assert await layer.get_element(seeded.thread_id, element.id) is None
    assert not (files_dir / object_key).exists()


async def test_get_thread_assembles_steps_and_elements(seeded: Seed):
    layer = seeded.layer
    thread = await layer.get_thread(seeded.thread_id)
    assert thread is not None
    assert thread["id"] == seeded.thread_id
    assert thread["tags"] == ["a"]
    assert len(thread["steps"]) == 1
    first_step = thread["steps"][0]
    assert first_step.get("id") == seeded.step_id
    assert first_step.get("feedback") is None
    assert thread["elements"] == []

    assert await layer.get_thread(str(uuid4())) is None


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
    # close() закрывает только storage (пул принадлежит DI) — не должен падать
    await layer.close()
