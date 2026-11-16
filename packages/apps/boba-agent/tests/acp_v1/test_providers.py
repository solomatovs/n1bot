from __future__ import annotations

import json
from uuid import uuid4

import pytest
from acp import RequestError, schema

from boba.agent import models as m
from boba.agent.queries import Query

from .client import V1System

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


async def test_new_user_adds_arbitrary_provider_and_keeps_private_configuration(
    system: V1System,
) -> None:
    owner = uuid4().hex
    client = await system.client(owner=owner)
    initial = await client.connection.send_request("providers/list", {})
    assert schema.ListProvidersResponse.model_validate(initial).providers == []
    sid = await client.new_session()
    model = await system.history.latest(sid, kind="model")
    assert m.ModelRecord.model_validate(model[0]).payload.provider is None
    url = system.inference.settings.base_url.replace("/v1", "/client/v1")
    response = await client.connection.send_request(
        "providers/set",
        {
            "providerId": "my-inference",
            "apiType": "openai",
            "baseUrl": url,
            "headers": {
                "Authorization": "Bearer client-test-key",
                "X-Client-Route": "custom",
            },
        },
    )
    schema.SetProviderResponse.model_validate(response)
    listed = await client.connection.send_request("providers/list", {})
    providers = schema.ListProvidersResponse.model_validate(listed).providers
    assert [item.provider_id for item in providers] == ["my-inference"]
    assert listed["providers"][0]["current"] == {"apiType": "openai", "baseUrl": url}
    assert "client-test-key" not in json.dumps(listed)
    assert "headers" not in json.dumps(listed)
    stored = await system.history.rows(Query.PROVIDER_GET, owner, "my-inference")
    encoded = json.dumps(stored[0]["configuration"])
    assert "client-test-key" not in encoded
    assert "enc:v1:" in encoded
    assert (
        await system.history.rows(Query.PROVIDER_GET, system.owner, "my-inference")
        == []
    )
    model = await system.history.latest(sid, kind="model")
    assert m.ModelRecord.model_validate(model[0]).payload.provider == "my-inference"

    await system.restart()
    restored = await system.client(owner=owner)
    await restored.resume(sid)
    system.inference.response = system.answer("Client endpoint")
    result = await restored.connection.send_request(
        "session/prompt",
        {"sessionId": sid, "prompt": [{"type": "text", "text": "Use my endpoint"}]},
    )
    assert schema.PromptResponse.model_validate(result).stop_reason == "end_turn"
    assert system.inference.request_paths[-1] == "/client/v1/chat/completions"
    assert (
        system.inference.request_headers[-1]["authorization"]
        == "Bearer client-test-key"
    )
    assert system.inference.request_headers[-1]["x-client-route"] == "custom"
    attempt = await system.history.latest(sid, kind="llm_request_attempt")
    assert (
        str(m.LlmRequestAttemptRecord.model_validate(attempt[0]).payload.url)
        == url + "/chat/completions"
    )

    other = await system.client(owner=uuid4().hex)
    listed = await other.connection.send_request("providers/list", {})
    assert schema.ListProvidersResponse.model_validate(listed).providers == []
    await other.connection.send_request(
        "providers/disable", {"providerId": "my-inference"}
    )
    await other.connection.send_request(
        "providers/set",
        {
            "providerId": "my-inference",
            "apiType": "openai",
            "baseUrl": system.inference.settings.base_url,
        },
    )
    other_sid = await other.new_session()
    result = await other.connection.send_request(
        "session/prompt",
        {
            "sessionId": other_sid,
            "prompt": [{"type": "text", "text": "Use my own endpoint"}],
        },
    )
    assert schema.PromptResponse.model_validate(result).stop_reason == "end_turn"
    assert system.inference.request_paths[-1] == "/v1/chat/completions"
    assert "authorization" not in system.inference.request_headers[-1]

    await restored.connection.send_request(
        "providers/set",
        {"providerId": "my-inference", "apiType": "openai", "baseUrl": url},
    )
    result = await restored.connection.send_request(
        "session/prompt",
        {
            "sessionId": sid,
            "prompt": [{"type": "text", "text": "Headers were removed"}],
        },
    )
    assert schema.PromptResponse.model_validate(result).stop_reason == "end_turn"
    assert "authorization" not in system.inference.request_headers[-1]
    assert "x-client-route" not in system.inference.request_headers[-1]


async def test_disabled_personal_provider_does_not_fall_back(system: V1System) -> None:
    client = await system.client()
    sid = await client.new_session()
    await client.connection.send_request("providers/disable", {"providerId": "test"})
    listed = await client.connection.send_request("providers/list", {})
    assert (
        schema.ListProvidersResponse.model_validate(listed).providers[0].current is None
    )
    with pytest.raises(RequestError):
        await client.connection.send_request(
            "session/prompt",
            {"sessionId": sid, "prompt": [{"type": "text", "text": "Disabled"}]},
        )
    assert system.inference.requests == []
    assert await system.history.latest(sid, kind="llm_request_attempt") == ()
    notices = await system.history.latest(sid, kind="notice")
    assert "disabled" in str(notices)
    await client.connection.send_request("providers/disable", {"providerId": "unknown"})
    await system.restart()
    restored = await system.client()
    listed = await restored.connection.send_request("providers/list", {})
    assert (
        schema.ListProvidersResponse.model_validate(listed).providers[0].current is None
    )


async def test_provider_selection_is_independent_of_model_profile(
    system: V1System,
) -> None:
    client = await system.client(granted=frozenset({"test", "alternative"}))
    sid = await client.new_session()
    url = system.inference.settings.base_url.replace("/v1", "/client/v1")
    await client.connection.send_request(
        "providers/set",
        {"providerId": "custom", "apiType": "openai", "baseUrl": url},
    )
    before = await system.history.latest(sid, kind="model")
    assert m.ModelRecord.model_validate(before[0]).payload.provider == "test"
    options = await client.connection.send_request(
        "session/set_config_option",
        {"sessionId": sid, "configId": "provider", "value": "custom"},
    )
    schema.SetSessionConfigOptionResponse.model_validate(options)
    await client.connection.send_request(
        "session/set_config_option",
        {"sessionId": sid, "configId": "model", "value": "alternative"},
    )
    model = await system.history.latest(sid, kind="model")
    assert m.ModelRecord.model_validate(model[0]).payload.provider == "custom"
    system.inference.response = system.answer("Selected")
    result = await client.connection.send_request(
        "session/prompt",
        {"sessionId": sid, "prompt": [{"type": "text", "text": "Selected provider"}]},
    )
    assert schema.PromptResponse.model_validate(result).stop_reason == "end_turn"
    assert system.inference.request_paths[-1] == "/client/v1/chat/completions"
    assert json.loads(system.inference.requests[-1])["model"] == "alternative-model"
    await system.restart()
    restored = await system.client(granted=frozenset({"test", "alternative"}))
    await restored.resume(sid)
    option = await system.history.latest(sid, kind="parameter_option", key="provider")
    choices = m.ParameterOptionRecord.model_validate(option[0]).payload.choices
    assert [choice.value_id for choice in choices] == ["custom", "test"]


@pytest.mark.parametrize(
    ("api_type", "url"),
    [
        ("anthropic", "https://example.invalid"),
        ("openai", "https://user:password@example.invalid"),
        ("openai", "https://:password@example.invalid"),
    ],
)
async def test_provider_rejects_invalid_configuration(
    system: V1System, api_type: str, url: str
) -> None:
    client = await system.client()
    with pytest.raises(RequestError) as failure:
        await client.connection.send_request(
            "providers/set",
            {"providerId": "anything", "apiType": api_type, "baseUrl": url},
        )

    assert failure.value.code == -32602


async def test_profiles_with_same_model_keep_distinct_selection(
    system: V1System,
) -> None:
    client = await system.client(granted=frozenset({"test", "same-model"}))
    sid = await client.new_session()
    response = await client.connection.send_request(
        "session/set_config_option",
        {"sessionId": sid, "configId": "model", "value": "same-model"},
    )
    schema.SetSessionConfigOptionResponse.model_validate(response)
    model = await system.history.latest(sid, kind="model")
    selected = m.ModelRecord.model_validate(model[0]).payload
    assert selected.profile_id == "same-model"
    assert selected.provider == "test"
    assert selected.value == {"model": "system-test-model"}
    system.inference.response = system.answer("Same model, different preset")
    response = await client.connection.send_request(
        "session/prompt",
        {"sessionId": sid, "prompt": [{"type": "text", "text": "Use selected preset"}]},
    )
    assert schema.PromptResponse.model_validate(response).stop_reason == "end_turn"
    sent = json.loads(system.inference.requests[-1])
    assert sent["model"] == "system-test-model"
    assert sent["temperature"] == 0.8
