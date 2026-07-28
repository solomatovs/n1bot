import json
from uuid import UUID, uuid4

import httpx
import pytest
from chainlit.types import Pagination, ThreadFilter
from chainlit.user import User as ChainlitUser
from psycopg_pool import AsyncConnectionPool

from boba.hermes.agent.api import HermesApi
from boba.hermes.chat.data import HermesDataLayer, HermesProfileRepository
from boba.hermes.chat.data.data_layer import PostgresDataLayer
from boba.hermes.infra.config import HermesConfig

pytestmark = pytest.mark.anyio

THREAD_ID = "6f1c0e3c-0000-4000-8000-000000000010"


class FakeHermes:
    """Мок api_server: помнит сессии и историю, отвечает как настоящий."""

    def __init__(self) -> None:
        self.sessions: dict[str, dict] = {}
        self.messages: dict[str, list[dict]] = {}
        self.profiles: set[str] = set()
        self.requests: list[tuple[str, str]] = []

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        # ветка на каждый эндпоинт: мок повторяет контракт api_server
        path = request.url.path
        self.requests.append((request.method, path))

        if path == "/api/profiles":
            return self._profile(json.loads(request.content))

        # /p/<профиль>/api/sessions[/<id>[/messages]]
        return self._session(request, path.split("/api/sessions", 1)[1].strip("/"))

    def _session(self, request: httpx.Request, tail: str) -> httpx.Response:  # noqa: PLR0911
        if request.method == "GET" and not tail:
            rows = list(self.sessions.values())
            return httpx.Response(
                200, json={"object": "list", "data": rows, "has_more": False}
            )

        if request.method == "POST" and not tail:
            body = json.loads(request.content)
            session_id = body["id"]
            if session_id in self.sessions:
                return httpx.Response(
                    409, json={"error": {"message": "Session already exists"}}
                )
            self.sessions[session_id] = {
                "id": session_id,
                "source": body.get("source") or "api_server",
                "started_at": 1785168444.0,
                "title": body.get("title"),
            }
            return httpx.Response(201, json={"session": self.sessions[session_id]})

        session_id = tail.split("/", maxsplit=1)[0]
        session = self.sessions.get(session_id)

        if session is None:
            return httpx.Response(
                404, json={"error": {"message": "Session not found"}}
            )

        if tail.endswith("/messages"):
            return httpx.Response(
                200,
                json={
                    "object": "list",
                    "session_id": session_id,
                    "data": self.messages.get(session_id, []),
                },
            )

        if request.method == "PATCH":
            body = json.loads(request.content)
            if "title" in body:
                taken = any(
                    other["id"] != session_id and other.get("title") == body["title"]
                    for other in self.sessions.values()
                )
                if taken:
                    return httpx.Response(
                        400,
                        json={
                            "error": {
                                "message": "Title already in use",
                                "code": "invalid_title",
                            }
                        },
                    )
                session["title"] = body["title"]
            return httpx.Response(200, json={"session": session})

        if request.method == "DELETE":
            self.sessions.pop(session_id)
            return httpx.Response(
                200, json={"object": "hermes.session.deleted", "id": session_id}
            )

        return httpx.Response(200, json={"session": session})

    def _profile(self, body: dict) -> httpx.Response:
        name = body["name"]
        if name in self.profiles:
            return httpx.Response(
                409,
                json={
                    "error": {
                        "message": f"Profile already exists: {name}",
                        "code": "profile_exists",
                    }
                },
            )
        self.profiles.add(name)

        return httpx.Response(
            201,
            json={
                "object": "hermes.profile",
                "profile": {
                    "name": name,
                    "path": f"/opt/data/profiles/{name}",
                    "clone_from": body.get("clone_from"),
                },
            },
        )


@pytest.fixture
def hermes() -> FakeHermes:
    return FakeHermes()


@pytest.fixture
async def layer_of(
    layer: PostgresDataLayer,
    pool: AsyncConnectionPool,
    test_schema: str,
    hermes: FakeHermes,
) -> HermesDataLayer:
    api = HermesApi(
        httpx.AsyncClient(transport=hermes.transport()),
        HermesConfig(base_url="http://hermes:8642", api_key="k" * 32),
    )

    return HermesDataLayer(
        storage=layer,
        profiles=HermesProfileRepository(pool, schema=test_schema),
        api=api,
    )


@pytest.fixture
async def user(layer_of: HermesDataLayer) -> ChainlitUser:
    created = await layer_of.create_user(ChainlitUser(identifier="solomatovs"))
    assert created is not None

    return created


async def test_create_user_binds_profile(
    layer_of: HermesDataLayer, user, pool: AsyncConnectionPool, test_schema: str
):
    profile = await HermesProfileRepository(pool, schema=test_schema).get(
        UUID(user.id)
    )

    # без профиля запросы к hermes уйдут в никуда
    assert profile == "solomatovs"


async def test_create_user_creates_profile_in_hermes(
    layer_of: HermesDataLayer, user, hermes: FakeHermes
):
    # профиль заводится тем же входом, что и пользователь: иначе первый же
    # запрос под /p/solomatovs/ получит 404 unknown profile
    assert hermes.profiles == {"solomatovs"}


async def test_second_login_does_not_fail_on_existing_profile(
    layer_of: HermesDataLayer, user, hermes: FakeHermes
):
    again = await layer_of.create_user(ChainlitUser(identifier="solomatovs"))

    # 409 от hermes значит, что состояние уже нужное
    assert again is not None
    assert hermes.profiles == {"solomatovs"}


async def test_update_thread_creates_session_in_hermes(
    layer_of: HermesDataLayer, user, hermes: FakeHermes
):
    await layer_of.update_thread(THREAD_ID, name="разбор логов", user_id=user.id)

    assert THREAD_ID in hermes.sessions
    assert hermes.sessions[THREAD_ID]["title"] == "разбор логов"


async def test_get_thread_reads_history_from_hermes(
    layer_of: HermesDataLayer, user, hermes: FakeHermes
):
    await layer_of.update_thread(THREAD_ID, name="разбор логов", user_id=user.id)
    hermes.messages[THREAD_ID] = [
        {
            "id": 1,
            "session_id": THREAD_ID,
            "role": "user",
            "timestamp": 1785168444.0,
            "content": "посмотри логи",
        },
        {
            "id": 2,
            "session_id": THREAD_ID,
            "role": "assistant",
            "timestamp": 1785168445.0,
            "content": "всё чисто",
        },
    ]

    thread = await layer_of.get_thread(THREAD_ID)

    assert thread is not None
    assert thread["name"] == "разбор логов"
    assert thread["userIdentifier"] == "solomatovs"
    assert [s.get("output") for s in thread["steps"]] == ["посмотри логи", "всё чисто"]


async def test_get_thread_returns_none_for_unknown(layer_of: HermesDataLayer):
    # тред не заводили: владельца нет, а значит и профиля для запроса
    assert await layer_of.get_thread(str(uuid4())) is None


async def test_list_threads_uses_profile_of_user(
    layer_of: HermesDataLayer, user, hermes: FakeHermes
):
    await layer_of.update_thread(THREAD_ID, name="первый", user_id=user.id)

    page = await layer_of.list_threads(
        Pagination(first=20), ThreadFilter(userId=user.id)
    )

    assert [t["name"] for t in page.data] == ["первый"]
    assert page.pageInfo.hasNextPage is False
    # запрос ушёл под профиль пользователя, а не в общий список
    sessions = [path for _, path in hermes.requests if "/api/sessions" in path]
    assert sessions
    assert all(path.startswith("/p/solomatovs/") for path in sessions)


async def test_list_threads_without_user_returns_empty(layer_of: HermesDataLayer):
    page = await layer_of.list_threads(Pagination(first=20), ThreadFilter())

    assert page.data == []


async def test_list_threads_filters_by_search(
    layer_of: HermesDataLayer, user, hermes: FakeHermes
):
    await layer_of.update_thread(THREAD_ID, name="разбор логов", user_id=user.id)
    second = str(uuid4())
    await layer_of.update_thread(second, name="про графану", user_id=user.id)

    page = await layer_of.list_threads(
        Pagination(first=20), ThreadFilter(userId=user.id, search="логов")
    )

    # поиска у hermes нет, фильтруем страницу сами
    assert [t["name"] for t in page.data] == ["разбор логов"]


async def test_delete_thread_removes_session(
    layer_of: HermesDataLayer, user, hermes: FakeHermes
):
    await layer_of.update_thread(THREAD_ID, name="разбор логов", user_id=user.id)

    await layer_of.delete_thread(THREAD_ID)

    assert THREAD_ID not in hermes.sessions
    assert await layer_of.get_thread(THREAD_ID) is None


async def test_duplicate_title_gets_numbered(
    layer_of: HermesDataLayer, user, hermes: FakeHermes
):
    await layer_of.update_thread(THREAD_ID, name="привет", user_id=user.id)
    second = str(uuid4())

    await layer_of.update_thread(second, name="привет", user_id=user.id)

    # заголовок у hermes уникален: два чата с одинаковым первым сообщением
    # это норма, поэтому второму достаётся "привет #2"
    assert hermes.sessions[second]["title"] == "привет #2"


async def test_create_step_writes_nothing(
    layer_of: HermesDataLayer, user, hermes: FakeHermes
):
    await layer_of.update_thread(THREAD_ID, name="разбор", user_id=user.id)
    before = len(hermes.requests)

    await layer_of.create_step(
        {"id": "s1", "threadId": THREAD_ID, "type": "user_message", "output": "привет"}
    )

    # историю пишет сам hermes во время хода: дублировать её нельзя
    assert len(hermes.requests) == before
    assert hermes.messages.get(THREAD_ID) is None
