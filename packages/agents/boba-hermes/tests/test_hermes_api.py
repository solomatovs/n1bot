import json

import httpx
import pytest

from boba.hermes.agent.api import HermesApi, HermesApiClient
from boba.hermes.agent.models import HermesRole
from boba.hermes.errors import ExternalServiceError, InternalServiceError
from boba.hermes.infra.config import HermesConfig

pytestmark = pytest.mark.anyio

PROFILE = "solomatovs"
SESSION = "6f1c0e3c-0000-4000-8000-000000000001"


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    """Клиент ходит только по http: chainlit-контекст и postgres ему не нужны.

    Подменяет autouse-фикстуру из conftest, которая поднимает их для тестов
    data layer.
    """


@pytest.fixture
def config() -> HermesConfig:
    return HermesConfig(base_url="http://hermes:8642/", api_key="k" * 32)


def _session_payload() -> dict:
    """Минимальная сессия: id, source и started_at у hermes NOT NULL."""
    return {"id": SESSION, "source": "api_server", "started_at": 1785168444.0}


def client_for(config: HermesConfig, handler) -> HermesApiClient:
    """Клиент поверх мока транспорта: наружу ни одного реального запроса."""
    transport = httpx.MockTransport(handler)
    return HermesApiClient(
        httpx.AsyncClient(transport=transport), config, profile=PROFILE
    )


async def test_requests_go_under_profile_prefix(config: HermesConfig):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"object": "list", "data": []})

    await client_for(config, handler).list_sessions(limit=50, offset=0)

    assert seen[0].url.path == f"/p/{PROFILE}/api/sessions"
    # без ключа api_server отвечает 401
    assert seen[0].headers["Authorization"] == f"Bearer {'k' * 32}"


async def test_list_sessions_passes_pagination(config: HermesConfig):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200, json={"object": "list", "data": [], "has_more": False}
        )

    page = await client_for(config, handler).list_sessions(limit=20, offset=40)

    assert dict(seen[0].url.params) == {"limit": "20", "offset": "40"}
    assert page.data == []
    assert page.has_more is False


async def test_get_session_returns_none_on_404(config: HermesConfig):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": {"message": "Session not found"}})

    assert await client_for(config, handler).get_session(SESSION) is None


async def test_create_session_returns_existing_on_conflict(config: HermesConfig):
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        if request.method == "POST":
            return httpx.Response(
                409, json={"error": {"message": "Session already exists"}}
            )
        return httpx.Response(
            200, json={"object": "hermes.session", "session": _session_payload()}
        )

    session = await client_for(config, handler).create_session(SESSION, title="тред")

    # 409 значит, что состояние уже нужное — наружу это не ошибка
    assert session.id == SESSION
    assert calls == ["POST", "GET"]


async def test_create_session_drops_empty_fields(config: HermesConfig):
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(201, json={"session": _session_payload()})

    await client_for(config, handler).create_session(
        SESSION, title=None, source="api_server"
    )

    # title=None у hermes очищает заголовок, а не «оставь как есть»
    assert seen[0] == {"id": SESSION, "source": "api_server"}


async def test_delete_session_is_idempotent(config: HermesConfig):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": {"message": "Session not found"}})

    assert await client_for(config, handler).delete_session(SESSION) is False


async def test_update_session_returns_none_on_404(config: HermesConfig):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": {"message": "Session not found"}})

    assert await client_for(config, handler).update_session(SESSION, title="x") is None


async def test_get_messages_returns_empty_on_404(config: HermesConfig):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": {"message": "Session not found"}})

    assert await client_for(config, handler).get_messages(SESSION) == []


async def test_client_error_becomes_internal(config: HermesConfig):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={
                "error": {
                    "message": "Title already in use",
                    "type": "invalid_request_error",
                    "param": None,
                    "code": "invalid_title",
                }
            },
        )

    with pytest.raises(InternalServiceError) as failure:
        await client_for(config, handler).update_session(SESSION, title="занято")

    # текст hermes нужен в логе, пользователю его не показываем
    # код ошибки hermes нужен в логе: по нему видно, что именно отвергнуто
    assert "Title already in use" in failure.value.internal_detail
    assert "invalid_title" in failure.value.internal_detail


async def test_server_error_becomes_external(config: HermesConfig):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            503, json={"error": {"message": "session db unavailable"}}
        )

    with pytest.raises(ExternalServiceError):
        await client_for(config, handler).list_sessions(limit=10, offset=0)


async def test_transport_failure_becomes_external(config: HermesConfig):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    with pytest.raises(ExternalServiceError):
        await client_for(config, handler).list_sessions(limit=10, offset=0)


async def test_non_json_error_body_is_reported(config: HermesConfig):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text="<html>bad gateway</html>")

    with pytest.raises(ExternalServiceError) as failure:
        await client_for(config, handler).list_sessions(limit=10, offset=0)

    assert "bad gateway" in failure.value.message


async def test_list_sessions_parses_models(config: HermesConfig):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "object": "list",
                "data": [
                    {
                        "id": SESSION,
                        "source": "api_server",
                        "started_at": 1785168444.0,
                        "title": "разбор логов",
                        "message_count": 4,
                        # поле, которого нет в модели: hermes волен добавлять свои
                        "_lineage_root_id": "x",
                    }
                ],
                "has_more": True,
            },
        )

    page = await client_for(config, handler).list_sessions(limit=1, offset=0)

    assert [s.title for s in page.data] == ["разбор логов"]
    assert page.data[0].message_count == 4
    assert page.has_more is True


async def test_get_messages_parses_tool_calls(config: HermesConfig):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "object": "list",
                "session_id": SESSION,
                "data": [
                    {
                        "id": 1,
                        "session_id": SESSION,
                        "role": "assistant",
                        "timestamp": 1785168444.0,
                        "content": None,
                        "tool_calls": [
                            {"id": "call-1", "function": {"name": "shell"}},
                            # вызов без id связать не с чем, он отбрасывается
                            {"function": {"name": "broken"}},
                        ],
                    }
                ],
            },
        )

    messages = await client_for(config, handler).get_messages(SESSION)

    assert [call.id for call in messages[0].tool_calls] == ["call-1"]
    assert messages[0].tool_calls[0].function.name == "shell"
    assert messages[0].role_of() is HermesRole.ASSISTANT


async def test_tool_call_parses_nested_function(config: HermesConfig):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "object": "list",
                "data": [
                    {
                        "id": 2,
                        "session_id": SESSION,
                        "role": "assistant",
                        "timestamp": 1785168444.0,
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_c5afa7",
                                # hermes дублирует id для сшивки с провайдером
                                "call_id": "call_c5afa7",
                                "response_item_id": "fc_c5afa7",
                                "type": "function",
                                "function": {
                                    "name": "terminal",
                                    "arguments": '{"command": "echo привет"}',
                                },
                            }
                        ],
                    }
                ],
            },
        )

    messages = await client_for(config, handler).get_messages(SESSION)
    call = messages[0].tool_calls[0]

    assert call.call_id == "call_c5afa7"
    assert call.response_item_id == "fc_c5afa7"
    assert call.function.name == "terminal"
    assert "echo" in call.function.arguments
    # у сообщения с вызовом инструмента текста нет
    assert messages[0].content is None


async def test_create_profile_goes_to_instance_root(config: HermesConfig):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            201,
            json={
                "object": "hermes.profile",
                "profile": {"name": PROFILE, "path": f"/opt/data/profiles/{PROFILE}"},
            },
        )

    api = HermesApi(httpx.AsyncClient(transport=httpx.MockTransport(handler)), config)

    assert await api.create_profile(PROFILE) is True
    # профиля ещё нет, поэтому запрос идёт мимо префикса /p/<профиль>/
    assert seen[0].url.path == "/api/profiles"
    # без донора у профиля не будет ни модели, ни ключа провайдера
    assert json.loads(seen[0].content) == {
        "name": PROFILE,
        "clone_from": config.default_profile,
    }


async def test_create_profile_returns_false_on_conflict(config: HermesConfig):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            409,
            json={
                "error": {
                    "message": f"Profile already exists: {PROFILE}",
                    "code": "profile_exists",
                }
            },
        )

    api = HermesApi(httpx.AsyncClient(transport=httpx.MockTransport(handler)), config)

    # состояние уже нужное — наружу это не ошибка
    assert await api.create_profile(PROFILE) is False


async def test_create_profile_rejects_bad_name(config: HermesConfig):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={
                "error": {
                    "message": "Invalid profile name",
                    "code": "invalid_profile_name",
                }
            },
        )

    api = HermesApi(httpx.AsyncClient(transport=httpx.MockTransport(handler)), config)

    with pytest.raises(InternalServiceError) as failure:
        await api.create_profile("../escape")

    assert "invalid_profile_name" in failure.value.internal_detail


async def test_error_without_param_is_parsed(config: HermesConfig):
    def handler(request: httpx.Request) -> httpx.Response:
        # у 401 ключа param нет вовсе
        return httpx.Response(
            401,
            json={
                "error": {
                    "message": "Invalid API key",
                    "type": "invalid_request_error",
                    "code": "invalid_api_key",
                }
            },
        )

    with pytest.raises(InternalServiceError) as failure:
        await client_for(config, handler).get_messages(SESSION)

    assert "Invalid API key" in failure.value.internal_detail
    assert "invalid_api_key" in failure.value.internal_detail
