"""Тесты PostgresDataLayer."""

from pathlib import Path
from uuid import uuid4

import pytest
from chainlit.element import CustomElement, Text
from chainlit.step import StepDict
from chainlit.types import Feedback as FeedbackPayload
from chainlit.types import Pagination, ThreadFilter
from chainlit.user import User as ChainlitUser
from conftest import Seed, use_session

from boba.canvas.keys import ObjectKey
from boba.chainlit.data.data_layer import PostgresDataLayer
from boba.chat.threads import DataRejectedError, DataUnavailableError
from boba.identity.session import UserMetadataField

pytestmark = pytest.mark.anyio


async def test_setup_is_idempotent(layer: PostgresDataLayer):
    await layer.setup()


async def test_create_and_get_user(layer: PostgresDataLayer):
    created = await layer.create_user(
        ChainlitUser(identifier="alice", metadata={"k": "v"})
    )
    if created is None:
        raise AssertionError("created is not None")
    if created.identifier != "alice":
        raise AssertionError('created.identifier == "alice"')

    fetched = await layer.get_user("alice")
    if fetched is None:
        raise AssertionError("fetched is not None")
    if fetched.id != created.id:
        raise AssertionError("fetched.id == created.id")

    if fetched.identifier != "alice":
        raise AssertionError('fetched.identifier == "alice"')

    if await layer.get_user("does-not-exist") is not None:
        raise AssertionError('await layer.get_user("does-not-exist") is None')


async def test_identifier_case_cannot_split_a_user(layer: PostgresDataLayer):
    """Канон логина ставит вход; хранилище лишь не даёт завести двойника.

    Второе написание того же логина означает, что мимо авторизатора прошёл
    неканоничный identifier: такое падает, а не сливается молча.
    """
    created = await layer.create_user(
        ChainlitUser(identifier="maksimov.ma", metadata={"roles": ["DEV"]})
    )
    if created is None:
        raise AssertionError("created is not None")

    with pytest.raises(DataUnavailableError):
        await layer.create_user(ChainlitUser(identifier="Maksimov.MA"))

    fetched = await layer.get_user("maksimov.ma")
    if fetched is None:
        raise AssertionError("канонный логин находится")

    if fetched.id != created.id:
        raise AssertionError("та же строка")

    if await layer.get_user("MAKSIMOV.MA") is not None:
        raise AssertionError("хранилище ищет ровно то, что дал вход")


async def test_create_user_keeps_the_sign_in_label_on_the_caller(
    layer: PostgresDataLayer,
) -> None:
    """Метка входа не хранится в users, но и не пропадает у вызывающего.

    Из этого же объекта выпускается JWT сессии: стерев метку, вход остался бы
    без делегированных кредов, хотя тикет уже захвачен.
    """
    user = ChainlitUser(
        identifier="krb-label",
        metadata={
            UserMetadataField.PROVIDER: "KerberosAuth",
            UserMetadataField.PRINCIPAL: "user@EXAMPLE.COM",
            UserMetadataField.TICKET: "sealed-ticket-of-this-sign-in",
            UserMetadataField.ROLES: ["read"],
        },
    )

    created = await layer.create_user(user)
    if created is None:
        raise AssertionError("user must be created")

    if user.metadata.get(UserMetadataField.TICKET) != "sealed-ticket-of-this-sign-in":
        raise AssertionError(f"label must survive persisting: {user.metadata}")

    if UserMetadataField.TICKET in created.metadata:
        raise AssertionError(f"label must not reach the users row: {created.metadata}")


async def test_update_thread_and_author(seeded: Seed):
    layer = seeded.layer
    author = await layer.get_thread_author(seeded.thread_id)
    if author != seeded.user.identifier:
        raise AssertionError("author == seeded.user.identifier")

    await layer.update_thread(seeded.thread_id, name="renamed")
    thread = await layer.get_thread(seeded.thread_id)
    if thread is None:
        raise AssertionError("thread is not None")
    if thread["name"] != "renamed":
        raise AssertionError('thread["name"] == "renamed"')
    if thread["userId"] != seeded.user.id:
        raise AssertionError('thread["userId"] == seeded.user.id')


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
    if thread is None:
        raise AssertionError("thread is not None")
    if not (all(s.get("id") != step["id"] for s in thread["steps"])):
        raise AssertionError('all(s.get("id") != step["id"] for s in thread["steps"])')
    if await layer.get_favorite_steps(seeded.user.id) != []:
        raise AssertionError("await layer.get_favorite_steps(seeded.user.id) == []")


async def test_upsert_and_delete_feedback(seeded: Seed):
    layer = seeded.layer
    feedback = FeedbackPayload(
        forId=seeded.answer_step_id,
        value=1,
        comment="nice",
        threadId=seeded.thread_id,
    )
    feedback_id = await layer.upsert_feedback(feedback)
    if not (feedback_id):
        raise AssertionError("feedback_id")

    thread = await layer.get_thread(seeded.thread_id)
    if thread is None:
        raise AssertionError("thread is not None")
    step = next(s for s in thread["steps"] if s.get("id") == seeded.answer_step_id)
    feedback_dict = step.get("feedback")
    if feedback_dict is None:
        raise AssertionError("feedback_dict is not None")
    if feedback_dict["value"] != 1:
        raise AssertionError('feedback_dict["value"] == 1')

    if await layer.delete_feedback(feedback_id) is not True:
        raise AssertionError("await layer.delete_feedback(feedback_id) is True")


async def test_create_get_delete_element(
    seeded: Seed, files_dir: Path, monkeypatch: pytest.MonkeyPatch
):
    layer = seeded.layer
    use_session(monkeypatch, user_id=seeded.user.id)

    element = Text(
        thread_id=seeded.thread_id,
        for_id=seeded.answer_step_id,
        name="note.txt",
        content="payload",
    )
    await layer.create_element(element)

    fetched = await layer.get_element(seeded.thread_id, element.id)
    if fetched is None:
        raise AssertionError("fetched is not None")
    if fetched.get("id") != element.id:
        raise AssertionError('fetched.get("id") == element.id')
    if not (fetched.get("url")):
        raise AssertionError('fetched.get("url")')

    # путь не хранится, а вычисляется по шаблону от пользователя сессии
    object_key = ObjectKey.build(
        seeded.user.id, seeded.thread_id, element.name, element.id
    ).render()
    if not (object_key.endswith("/upload/note.txt")):
        raise AssertionError('object_key.endswith("/upload/note.txt")')
    if (files_dir / object_key).read_bytes() != b"payload":
        raise AssertionError('(files_dir / object_key).read_bytes() == b"payload"')

    await layer.delete_element(element.id)
    if await layer.get_element(seeded.thread_id, element.id) is not None:
        raise AssertionError("await layer.get_element(seeded.thread_id, element.id) i…")
    if (files_dir / object_key).exists():
        raise AssertionError("not (files_dir / object_key).exists()")


async def test_element_uploaded_by_route_keeps_its_stored_content(
    seeded: Seed, files_dir: Path, monkeypatch: pytest.MonkeyPatch
):
    """Вложение пользователя уже в хранилище: слой пишет строку и не трогает файл."""
    layer = seeded.layer
    use_session(monkeypatch, user_id=seeded.user.id)

    # так выглядит element после загрузки: путь из реестра сессии, копии на диске нет
    element = Text(
        thread_id=seeded.thread_id,
        for_id=seeded.answer_step_id,
        name="report.txt",
        path=str(files_dir / "session-files" / "missing.txt"),
    )
    object_key = ObjectKey.build(
        seeded.user.id, seeded.thread_id, element.name, element.id
    ).render()

    stored = files_dir / object_key
    stored.parent.mkdir(parents=True, exist_ok=True)
    stored.write_bytes(b"streamed by the upload route")

    await layer.create_element(element)

    fetched = await layer.get_element(seeded.thread_id, element.id)
    if fetched is None:
        raise AssertionError("fetched is not None")
    if stored.read_bytes() != b"streamed by the upload route":
        raise AssertionError('stored.read_bytes() == b"streamed by the upload route"')


async def test_custom_element_keeps_props_out_of_storage(
    seeded: Seed, files_dir: Path, monkeypatch: pytest.MonkeyPatch
):
    """Кастом-элемент несёт только props: тела в хранилище у него нет.

    Копию читать некому — лента берёт props из колонки, — а каждая запись
    стоит монтирования образа пользователя.
    """
    layer = seeded.layer
    use_session(monkeypatch, user_id=seeded.user.id)

    element = CustomElement(
        thread_id=seeded.thread_id,
        for_id=seeded.answer_step_id,
        name="CanvasStream",
        props={"call_id": "call-1", "label": "bash"},
    )
    await layer.create_element(element)

    fetched = await layer.get_element(seeded.thread_id, element.id)
    if fetched is None:
        raise AssertionError("fetched is not None")
    if fetched.get("props") != {"call_id": "call-1", "label": "bash"}:
        raise AssertionError('fetched.get("props") == {"call_id": "call-1", "label": …')

    object_key = ObjectKey.build(
        seeded.user.id, seeded.thread_id, element.name, element.id
    ).render()
    if (files_dir / object_key).exists():
        raise AssertionError("not (files_dir / object_key).exists()")


async def test_get_thread_builds_steps_from_history(seeded: Seed):
    layer = seeded.layer
    thread = await layer.get_thread(seeded.thread_id)
    if thread is None:
        raise AssertionError("thread is not None")
    if thread["id"] != seeded.thread_id:
        raise AssertionError('thread["id"] == seeded.thread_id')
    if thread["tags"] != ["a"]:
        raise AssertionError('thread["tags"] == ["a"]')

    outputs = [(s.get("type"), s.get("output")) for s in thread["steps"]]
    if outputs != [("user_message", "hi"), ("assistant_message", "hello")]:
        raise AssertionError('outputs == [("user_message", "hi"), ("assistant_message…')
    answer = thread["steps"][1]
    if answer.get("id") != seeded.answer_step_id:
        raise AssertionError('answer.get("id") == seeded.answer_step_id')
    if answer.get("feedback") is not None:
        raise AssertionError('answer.get("feedback") is None')
    if thread["elements"] != []:
        raise AssertionError('thread["elements"] == []')

    if await layer.get_thread(str(uuid4())) is not None:
        raise AssertionError("await layer.get_thread(str(uuid4())) is None")


async def test_thread_without_history_has_no_steps(seeded: Seed):
    layer = seeded.layer
    await layer.update_thread(str(uuid4()), user_id=seeded.user.id)
    threads = await layer.list_threads(
        Pagination(first=10), ThreadFilter(userId=seeded.user.id)
    )
    empty = next(t for t in threads.data if t["id"] != seeded.thread_id)
    thread = await layer.get_thread(empty["id"])
    if thread is None:
        raise AssertionError("thread is not None")
    if thread["steps"] != []:
        raise AssertionError('thread["steps"] == []')


async def test_list_threads(seeded: Seed):
    layer = seeded.layer
    page = await layer.list_threads(
        Pagination(first=10),
        ThreadFilter(userId=seeded.user.id),
    )
    if not (any(t["id"] == seeded.thread_id for t in page.data)):
        raise AssertionError('any(t["id"] == seeded.thread_id for t in page.data)')

    with pytest.raises(DataRejectedError, match="userId is required"):
        await layer.list_threads(Pagination(first=10), ThreadFilter())


async def test_delete_thread(seeded: Seed):
    layer = seeded.layer
    await layer.delete_thread(seeded.thread_id)
    if await layer.get_thread(seeded.thread_id) is not None:
        raise AssertionError("await layer.get_thread(seeded.thread_id) is None")


async def test_build_debug_url(layer: PostgresDataLayer):
    if await layer.build_debug_url() != "":
        raise AssertionError('await layer.build_debug_url() == ""')


async def test_close_releases_storage(layer: PostgresDataLayer):
    await layer.close()
