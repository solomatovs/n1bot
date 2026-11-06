"""Тесты PostgresDataLayer."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest
from aid_stand import AuthTestClaim as UserMetadataField
from aid_stand import FixedServers, NoTabs, Seed, SessionStub, StandTokens
from chainlit.step import StepDict
from chainlit.types import Feedback as FeedbackPayload
from chainlit.types import Pagination, ThreadFilter
from chainlit.user import PersistedUser
from chainlit.user import User as ChainlitUser
from fastapi import FastAPI

from boba.aid.auth.chainlit_user import SessionUser
from boba.aid.auth.composite import SessionCallback
from boba.aid.auth.policy import SessionRenewal
from boba.aid.auth.session import BrowserSessions
from boba.aid.config.app import AppConfig
from boba.aid.data.data_layer import PostgresDataLayer
from boba.aid.data.feedbacks import FeedbacksTable
from boba.aid.data.remote_storage import FileOwners, RemoteStorageClient
from boba.aid.data.threads import ThreadsTable
from boba.aid.data.users import UsersTable
from boba.aid.domain.fields import ThreadMetaField
from boba.aid.infra.session import ChainlitSession, ChainlitSessions
from boba.auth import SessionService, SignIns
from boba.canvas.keys import ObjectKey, ScopeDir
from boba.chat.messages import FeedbackChanged, ThreadActive, ThreadChanged
from boba.chat.profiles import ChatProfiles
from boba.chat.threads import DataRejectedError, ThreadUpsert
from boba.db.postgres import AsyncPostgresPool
from boba.identity.context import Scope
from boba.identity.principal import UserIdentity
from boba.mcp_client.client import FixedServerConfigs, McpServers
from boba.messaging import Envelope, MemoryMessageBus

pytestmark = pytest.mark.anyio


async def _project_signed_in_user(
    layer: PostgresDataLayer,
    users: UsersTable,
    user: ChainlitUser,
) -> PersistedUser:
    """Register the authenticated identity, then ask Chainlit for its projection."""
    await users.upsert(UserIdentity.model_validate(user.identifier))
    projected = await layer.create_user(user)
    if projected is None:
        raise AssertionError("a registered identity has a Chainlit projection")
    return projected


@pytest.fixture
def session_callback(
    app_config: AppConfig,
    pool: AsyncPostgresPool,
    monkeypatch: pytest.MonkeyPatch,
) -> SessionCallback:
    """Chainlit callback: JWT claims plus a UI projection from the users row."""
    import chainlit.auth as chainlit_auth

    session = app_config.session
    auth = BrowserSessions(
        sign_ins=SignIns(None, None, None),
        tokens=StandTokens.tokens(),
        cookie=session.cookie_spec(),
        sessions=SessionService(
            UsersTable(pool, app_config.db_schema), ChatProfiles(app_config.profiles)
        ),
        renewal=SessionRenewal.of(session.session_ttl_sec, session.session_max_sec),
    )

    callback = SessionCallback(auth, ChainlitSessions(StandTokens()))
    monkeypatch.setattr(chainlit_auth, "authenticate_user", callback.authenticate)

    return callback


async def test_create_and_get_user(
    layer: PostgresDataLayer,
    pool: AsyncPostgresPool,
    app_config: AppConfig,
):
    created = await _project_signed_in_user(
        layer,
        UsersTable(pool, app_config.db_schema),
        StandTokens.user(identifier="alice", metadata={"k": "v"}),
    )
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


async def test_identifier_case_cannot_split_a_user(
    layer: PostgresDataLayer,
    pool: AsyncPostgresPool,
    app_config: AppConfig,
):
    """Слой данных — граница chainlit: identifier из его cl.User канонизируется.

    Chainlit отдаёт логин как он записан в JWT, и токен партнёра, выпущенный
    по секрету, несёт любой регистр. Второе написание попадает в ту же
    строку users, а не заводит двойника и не падает на индексе.
    """
    # Identity storage canonicalizes the login during sign-in; Chainlit only
    # projects the already registered row for either spelling.
    users = UsersTable(pool, app_config.db_schema)
    await users.upsert(UserIdentity.model_validate("maksimov.ma"))
    created = await layer.create_user(StandTokens.user("maksimov.ma"))
    if created is None:
        raise AssertionError("registered identity has a Chainlit projection")

    again = await layer.create_user(StandTokens.user(identifier="Maksimov.MA"))
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


async def test_signed_session_uses_the_canonical_user_row(
    layer: PostgresDataLayer,
    session_callback: SessionCallback,
    pool: AsyncPostgresPool,
    app_config: AppConfig,
) -> None:
    """JWT с известной личностью использует каноничную строку users."""
    from chainlit.auth import authenticate_user

    first = await _project_signed_in_user(
        layer,
        UsersTable(pool, app_config.db_schema),
        StandTokens.user(identifier="ivanov.ii"),
    )

    partner_token = StandTokens.issue(StandTokens.session("Ivanov.II", UUID(first.id)))
    user = await authenticate_user(partner_token)

    assert isinstance(user, PersistedUser)
    if user.id != first.id:
        raise AssertionError("signed identity resolves to the same users row")

    if user.identifier != "ivanov.ii":
        raise AssertionError(f"session identifier is canonical: {user.identifier}")


async def test_create_user_keeps_the_sign_in_label_on_the_caller(
    layer: PostgresDataLayer,
    pool: AsyncPostgresPool,
    app_config: AppConfig,
) -> None:
    """Метка входа не хранится в users, но и не пропадает у вызывающего.

    Из этого же объекта выпускается JWT сессии: стерев метку, вход остался бы
    без делегированных кредов, хотя тикет уже захвачен.
    """
    user = StandTokens.user(
        identifier="krb-label",
        metadata={
            UserMetadataField.PROVIDER: "KerberosAuth",
            UserMetadataField.PRINCIPAL: "user@EXAMPLE.COM",
            UserMetadataField.TICKET: "sealed-ticket-of-this-sign-in",
            UserMetadataField.ROLES: ["read"],
        },
    )

    created = await _project_signed_in_user(
        layer, UsersTable(pool, app_config.db_schema), user
    )

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
    threads = ThreadsTable(pool, app_config.db_schema)
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


async def test_list_threads_pages_by_activity(seeded: Seed):
    """Страницы по курсору (updated_at, id): тред, ставший активным, — первым,
    остальные по убыванию активности; прокрутка до конца без повторов и
    пропусков, hasNextPage честный."""
    layer = seeded.layer
    created: list[str] = []
    for index in range(7):
        thread_id = str(uuid4())
        await layer.update_thread(thread_id, name=f"t{index}", user_id=seeded.user.id)
        created.append(thread_id)

    woken = created[2]
    await layer.thread_active(woken, datetime.now(UTC) + timedelta(minutes=5))

    shown: list[str] = []
    stamps: list[str] = []
    cursor: str | None = None
    pages = 0
    while True:
        page = await layer.list_threads(
            Pagination(first=3, cursor=cursor), ThreadFilter(userId=seeded.user.id)
        )
        pages += 1
        for thread in page.data:
            shown.append(thread["id"])
            stamps.append(thread["updatedAt"])

        if not page.pageInfo.hasNextPage:
            break

        assert len(page.data) == 3
        cursor = page.pageInfo.endCursor

    assert shown[0] == woken
    assert len(shown) == len(set(shown))
    assert set(created) <= set(shown)
    assert seeded.thread_id in shown
    assert pages == (len(shown) + 2) // 3

    moments: list[datetime] = []
    for stamp in stamps:
        moments.append(datetime.fromisoformat(stamp))

    assert moments == sorted(moments, reverse=True)

    fresh = await layer.get_thread(created[0])
    assert fresh is not None
    assert fresh["updatedAt"] == fresh["createdAt"]

    with pytest.raises(DataRejectedError, match="thread list cursor"):
        await layer.list_threads(
            Pagination(first=3, cursor="garbage"), ThreadFilter(userId=seeded.user.id)
        )


async def test_thread_activity_is_published_to_the_user_scope(
    layer: PostgresDataLayer, seeded: Seed, data_bus: MemoryMessageBus
) -> None:
    """Запись истории агента сдвигает время треда вперёд, и вкладки владельца
    получают значение базы; время не новее — ни записи, ни сообщения."""
    seen: list[Envelope] = []

    async def collect(envelope: Envelope) -> None:
        seen.append(envelope)

    later = datetime.now(UTC) + timedelta(minutes=10)
    leave = data_bus.subscribe(Scope.user(UUID(seeded.user.id)), collect)
    try:
        await layer.thread_active(seeded.thread_id, later)
        await layer.thread_active(seeded.thread_id, later - timedelta(minutes=1))
        await layer.thread_active(str(uuid4()), later)
    finally:
        leave()

    active: list[ThreadActive] = []
    for envelope in seen:
        message = envelope.message
        assert isinstance(message, ThreadActive)
        active.append(message)

    assert active == [ThreadActive(thread_id=seeded.thread_id, updated_at=later)]

    thread = await layer.get_thread(seeded.thread_id)
    assert thread is not None
    assert datetime.fromisoformat(thread["updatedAt"]) == later


async def test_delete_thread_removes_its_files(seeded: Seed, files_dir: Path):
    """Удаление треда стирает его файлы в хранилище владельца во всех каталогах
    треда."""
    layer = seeded.layer
    stored: list[Path] = []
    for scope_dir in ScopeDir:
        key = ObjectKey.build(
            seeded.user.id, seeded.thread_id, "gone.txt", "el", scope_dir=scope_dir
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
    """Пользователю не выдан ни один сервер таблицы mcp_servers — хранилища
    файлов у него нет: удаление треда не падает, удалять нечего."""
    users = UsersTable(pool, app_config.db_schema)
    sessions = NoTabs()
    servers = McpServers(FixedServerConfigs(servers={}))
    owners = FileOwners(lambda: sessions, lambda: users, lambda: FixedServers({}))
    bare = PostgresDataLayer(
        users=users,
        threads=ThreadsTable(pool, app_config.db_schema),
        feedbacks=FeedbacksTable(pool, app_config.db_schema),
        storage=RemoteStorageClient("/workspace", owners, lambda: servers),
        bus=data_bus,
    )
    identity = UserIdentity.model_validate("bare-user")
    await users.upsert(identity)
    user = await bare.create_user(
        StandTokens.user(
            identifier=identity.identifier,
            metadata={UserMetadataField.ROLES: ["dev"]},
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

    kinds = [envelope.message.kind for envelope in seen]
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


async def test_chainlit_keeps_the_signed_role_snapshot_without_database_reads(
    layer: PostgresDataLayer,
    app_config: AppConfig,
    pool: AsyncPostgresPool,
    session_callback: SessionCallback,
) -> None:
    from chainlit.auth import authenticate_user

    from boba.stand.roles import RoleSeed

    users = UsersTable(pool, app_config.db_schema)
    metadata: dict[str, object] = {
        "provider": "KerberosAuth",
        "principal": "alice@EXAMPLE.COM",
        "sso_ticket": "ticket-for-this-session",
        "generation": StandTokens.GENERATION,
        "roles": ["read"],
        "profiles": ["general"],
        "profile": "general",
    }
    transient = StandTokens.user(
        identifier="session-fields",
        metadata=metadata,
    )
    await users.upsert(UserIdentity.model_validate(transient.identifier))
    created = await layer.create_user(transient)
    assert created is not None
    await RoleSeed(users).add(["read"])
    query = (
        users._query()
        .add(
            "insert into {schema}.user_roles (user_id, role_id) "
            "select %(id)s, id from {schema}.roles where role = 'read' "
            "on conflict (user_id, role_id) do update set managed = false",
            id=UUID(created.id),
        )
        .build()
    )
    await users._execute(query, "administrator assigns role")
    token = StandTokens.issue(
        StandTokens.session_from_metadata(
            "session-fields",
            UUID(created.id),
            metadata,
            profiles={"general"},
        )
    )
    authenticated = await authenticate_user(token)
    assert isinstance(authenticated, SessionUser)
    assert authenticated.metadata == {}
    assert authenticated.principal.roles == {"read"}
    assert authenticated.principal.profiles == {"general"}
    authenticated_ticket = StandTokens.tokens().read(token).ticket()
    assert authenticated_ticket is not None
    assert authenticated_ticket.sealed == "ticket-for-this-session"
    query = (
        users._query()
        .add(
            "delete from {schema}.user_roles where user_id = %(id)s",
            id=UUID(created.id),
        )
        .build()
    )
    await users._execute(query, "administrator removes membership")
    revoked = await authenticate_user(token)
    assert isinstance(revoked, SessionUser)
    assert revoked.metadata == {}
    assert revoked.principal.roles == {"read"}
    revoked_ticket = StandTokens.tokens().read(token).ticket()
    assert revoked_ticket is not None
    assert revoked_ticket.sealed == "ticket-for-this-session"
    stub = SessionStub(created.id, None, identifier=transient.identifier)
    stub.user = PersistedUser(
        id=revoked.id,
        identifier=revoked.identifier,
        createdAt=revoked.createdAt,
        metadata={},
    )
    stub.token = token
    active = ChainlitSession(stub, StandTokens()).signed_in().session()
    assert active.principal.roles == frozenset({"read"})
    ticket = active.ticket()
    assert ticket is not None
    assert ticket.sealed == "ticket-for-this-session"


async def test_chainlit_accepts_a_valid_token_without_reading_or_creating_a_user_row(
    app_config: AppConfig,
    pool: AsyncPostgresPool,
    session_callback: SessionCallback,
) -> None:
    token = StandTokens.issue(StandTokens.session("missing-user", UUID(int=99)))
    claims = StandTokens.tokens().read(token)
    assert claims.identifier == "missing-user"
    assert claims.user_id == UUID(int=99)
    users = UsersTable(pool, app_config.db_schema)
    assert await users.stored_by_id(UUID(int=99)) is None


async def test_auth_jwt_route_checks_and_reuses_the_session_token(
    layer: PostgresDataLayer,
    session_callback: SessionCallback,
    pool: AsyncPostgresPool,
    app_config: AppConfig,
) -> None:
    user = StandTokens.user("jwt-exchange")
    created = await _project_signed_in_user(
        layer, UsersTable(pool, app_config.db_schema), user
    )
    token = StandTokens.issue(StandTokens.session(user.identifier, UUID(created.id)))
    app = FastAPI()
    session_callback.install(app)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://stand"
    ) as client:
        response = await client.post(
            "/auth/jwt", headers={"Authorization": f"Bearer {token}"}
        )

    assert response.status_code == 200
    cookie = response.headers["set-cookie"]
    assert cookie.split(";", 1)[0].endswith(f"={token}")
