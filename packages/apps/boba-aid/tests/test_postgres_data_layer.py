"""Тесты PostgresDataLayer."""

from pathlib import Path
from uuid import UUID, uuid4

import pytest
from aid_stand import NoTabs, Seed
from chainlit.step import StepDict
from chainlit.types import Feedback as FeedbackPayload
from chainlit.types import Pagination, ThreadFilter
from chainlit.user import PersistedUser
from chainlit.user import User as ChainlitUser

from boba.aid.data.data_layer import PostgresDataLayer
from boba.aid.data.remote_storage import FileOwners, RemoteStorageClient
from boba.aid.domain.fields import ThreadMetaField
from boba.aid.infra.config import AppConfig
from boba.canvas.keys import ObjectKey, ThreadDir
from boba.chat.profiles import ChatProfileConfig, ChatProfiles
from boba.chat.threads import DataRejectedError, ThreadUpsert
from boba.db.postgres import AsyncPostgresPool
from boba.identity.context import Scope
from boba.identity.session import UserMetadataField
from boba.mcp_client.client import McpServers, McpServersConfig
from boba.messaging import (
    Envelope,
    FeedbackChanged,
    MemoryMessageBus,
    ThreadChanged,
)
from boba.runtime.elements import FeedbacksTable
from boba.runtime.threads import ThreadsTable
from boba.runtime.users import UsersTable

pytestmark = pytest.mark.anyio


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
    """Слой данных — граница chainlit: identifier из его cl.User канонизируется.

    Chainlit отдаёт логин как он записан в JWT, и токен партнёра, выпущенный
    по секрету, несёт любой регистр. Второе написание попадает в ту же
    строку users, а не заводит двойника и не падает на индексе.
    """
    created = await layer.create_user(
        ChainlitUser(identifier="maksimov.ma", metadata={"roles": ["DEV"]})
    )
    if created is None:
        raise AssertionError("created is not None")

    again = await layer.create_user(ChainlitUser(identifier="Maksimov.MA"))
    if again is None:
        raise AssertionError("second spelling is created")

    if again.id != created.id:
        raise AssertionError("second spelling merges into the same users row")

    if again.identifier != "maksimov.ma":
        raise AssertionError(f"persisted identifier is canonical: {again.identifier}")

    fetched = await layer.get_user("MAKSIMOV.MA")
    if fetched is None:
        raise AssertionError("lookup by another spelling finds the row")

    if fetched.id != created.id:
        raise AssertionError("same row")


async def test_partner_jwt_signs_in_as_the_canonical_user(
    layer: PostgresDataLayer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Путь /auth/jwt и подключения сокета: chainlit сам декодирует JWT партнёра
    и зовёт authenticate_user; на выходе — строка users с каноничным логином."""
    import chainlit.data as chainlit_data
    from chainlit.auth import authenticate_user
    from chainlit.auth.jwt import create_jwt

    monkeypatch.setattr(chainlit_data, "_data_layer", layer)
    monkeypatch.setattr(chainlit_data, "_data_layer_initialized", True)

    first = await layer.create_user(ChainlitUser(identifier="ivanov.ii"))
    if first is None:
        raise AssertionError("first sign-in is created")

    partner_token = create_jwt(ChainlitUser(identifier="Ivanov.II"))
    user = await authenticate_user(partner_token)

    if not isinstance(user, PersistedUser):
        raise AssertionError(f"authenticate_user returns the persisted user: {user!r}")

    if user.id != first.id:
        raise AssertionError("partner spelling signs in as the same users row")

    if user.identifier != "ivanov.ii":
        raise AssertionError(f"session identifier is canonical: {user.identifier}")


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


async def test_update_thread_leaves_the_agent_session_to_the_agent(
    app_config: AppConfig, pool: AsyncPostgresPool, seeded: Seed
):
    """Сессию агента в threads.meta пишет только агент треда: persist сессии
    chainlit на отключении вкладки не возвращает тред на прежнюю сессию и не
    стирает указатель."""
    layer = seeded.layer
    threads = ThreadsTable(app_config.postgres, app_config.db_schema, pool)
    await threads.upsert(
        ThreadUpsert(
            id=UUID(seeded.thread_id),
            meta_set={ThreadMetaField.AGENT_SESSION: "branch"},
        )
    )

    await layer.update_thread(
        seeded.thread_id,
        metadata={ThreadMetaField.AGENT_SESSION: "original", "topic": "y"},
    )
    await layer.update_thread(
        seeded.thread_id, metadata={ThreadMetaField.AGENT_SESSION: None}
    )

    thread = await layer.get_thread(seeded.thread_id)
    assert thread is not None
    meta = thread["metadata"]
    assert meta is not None
    assert meta[ThreadMetaField.AGENT_SESSION] == "branch"
    assert meta["topic"] == "y"


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


async def test_deleting_a_step_keeps_its_feedback(seeded: Seed):
    """Снятие шага в интерфейсе (правка вопроса, Message.remove) данных беседы
    не трогает: оценки ходов удаляются только вместе с тредом."""
    layer = seeded.layer
    await layer.upsert_feedback(
        FeedbackPayload(forId=seeded.answer_step_id, value=1, threadId=seeded.thread_id)
    )

    await layer.delete_step(seeded.answer_step_id)

    assert seeded.answer_step_id in await layer.feedbacks_of(seeded.thread_id)


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

    # шагов у треда нет: оценки лента получает по id шага после воспроизведения
    rated = await layer.feedbacks_of(seeded.thread_id)
    feedback_dict = rated.get(seeded.answer_step_id)
    if feedback_dict is None:
        raise AssertionError("feedback_dict is not None")
    if feedback_dict["value"] != 1:
        raise AssertionError('feedback_dict["value"] == 1')

    if await layer.delete_feedback(feedback_id) is not True:
        raise AssertionError("await layer.delete_feedback(feedback_id) is True")

    if await layer.feedbacks_of(seeded.thread_id) != {}:
        raise AssertionError("the deleted feedback is gone")


async def test_get_thread_serves_no_steps(seeded: Seed):
    """История живёт у агента: тред отдаётся без шагов, их воспроизводит лента."""
    layer = seeded.layer
    thread = await layer.get_thread(seeded.thread_id)
    if thread is None:
        raise AssertionError("thread is not None")
    if thread["id"] != seeded.thread_id:
        raise AssertionError('thread["id"] == seeded.thread_id')
    if thread["tags"] != ["a"]:
        raise AssertionError('thread["tags"] == ["a"]')

    if thread["steps"] != []:
        raise AssertionError('thread["steps"] == []')
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


async def test_delete_thread_removes_its_files(seeded: Seed, files_dir: Path):
    """Удаление треда стирает его файлы в хранилище владельца во всех каталогах
    треда."""
    layer = seeded.layer
    stored: list[Path] = []
    for dir_thread in ThreadDir:
        key = ObjectKey.build(
            seeded.user.id, seeded.thread_id, "gone.txt", "el", dir_thread=dir_thread
        )
        path = files_dir / key.render()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"gone")
        stored.append(path)

    await layer.delete_thread(seeded.thread_id)

    assert await layer.get_thread(seeded.thread_id) is None
    for path in stored:
        assert not path.exists(), path


async def test_delete_thread_of_a_user_without_file_storage(
    app_config: AppConfig,
    pool: AsyncPostgresPool,
    layer: PostgresDataLayer,
    data_bus: MemoryMessageBus,
):
    """Профиль пользователя не называет MCP-серверов — хранилища файлов у него
    нет: удаление треда не падает, удалять из хранилища нечего."""
    users = UsersTable(app_config.postgres, app_config.db_schema, pool)
    profiles = ChatProfiles(
        {"bare": ChatProfileConfig.model_construct(mcp=[], roles=["*"], default=True)}
    )
    sessions = NoTabs()
    servers = McpServers(McpServersConfig(servers={}))
    owners = FileOwners(lambda: sessions, lambda: users, profiles)
    bare = PostgresDataLayer(
        users=users,
        threads=ThreadsTable(app_config.postgres, app_config.db_schema, pool),
        feedbacks=FeedbacksTable(app_config.postgres, app_config.db_schema, pool),
        storage=RemoteStorageClient("/workspace", owners, lambda: servers),
        bus=data_bus,
    )
    user = await bare.create_user(
        ChainlitUser(
            identifier="bare-user",
            metadata={
                UserMetadataField.ROLES: ["dev"],
                UserMetadataField.PROFILES: ["bare"],
            },
        )
    )
    assert user is not None
    thread_id = str(uuid4())
    await bare.update_thread(thread_id, name="bare", user_id=user.id)

    await bare.delete_thread(thread_id)

    assert await layer.get_thread(thread_id) is None


async def test_build_debug_url(layer: PostgresDataLayer):
    if await layer.build_debug_url() != "":
        raise AssertionError('await layer.build_debug_url() == ""')


async def test_close_releases_storage(layer: PostgresDataLayer):
    await layer.close()


async def test_thread_changes_are_published_to_the_user_scope(
    layer: PostgresDataLayer, seeded: Seed, data_bus: MemoryMessageBus
) -> None:
    """Создание, переименование и удаление треда уходят в область пользователя;
    правка одних метаданных списку не видна.
    """
    seen: list[Envelope] = []

    async def collect(envelope: Envelope) -> None:
        seen.append(envelope)

    leave = data_bus.subscribe(Scope.user(UUID(seeded.user.id)), collect)
    thread_id = str(uuid4())
    try:
        await layer.update_thread(thread_id, name="first", user_id=seeded.user.id)
        await layer.update_thread(thread_id, metadata={"topic": "x"})
        await layer.update_thread(thread_id, name="renamed")
        await layer.delete_thread(thread_id)
    finally:
        leave()

    changes: list[tuple[str, str, str]] = []
    for envelope in seen:
        message = envelope.message
        assert isinstance(message, ThreadChanged)
        changes.append((message.thread_id, message.action.value, message.name))

    assert changes == [
        (thread_id, "created", "first"),
        (thread_id, "updated", "renamed"),
        (thread_id, "deleted", ""),
    ]


async def test_feedback_changes_reach_the_thread_scope(
    layer: PostgresDataLayer, seeded: Seed, data_bus: MemoryMessageBus
) -> None:
    """Оценка ответа и её снятие уходят в область треда: вкладки на всех
    инстансах обновляют шаг.
    """
    seen: list[Envelope] = []

    async def collect(envelope: Envelope) -> None:
        seen.append(envelope)

    leave = data_bus.subscribe(Scope.chat(seeded.thread_id), collect)
    try:
        feedback_id = await layer.upsert_feedback(
            FeedbackPayload(
                forId=seeded.answer_step_id,
                threadId=seeded.thread_id,
                value=1,
                comment="nice",
            )
        )
        assert await layer.delete_feedback(feedback_id) is True
        assert await layer.delete_feedback(feedback_id) is False
    finally:
        leave()

    kinds = [envelope.message.kind.value for envelope in seen]
    assert kinds == ["feedback_changed", "feedback_changed"]
    first = seen[0].message
    assert isinstance(first, FeedbackChanged)
    assert (first.step_id, first.value, first.comment) == (
        seeded.answer_step_id,
        1,
        "nice",
    )
    second = seen[1].message
    assert isinstance(second, FeedbackChanged)
    assert second.value is None
