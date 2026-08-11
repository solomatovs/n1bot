"""Узлы web в настоящей песочнице: страница скачивается и ищется оттуда.

Секреты профиля проверяются на живом сервере: страница отдаётся только по
basic-auth, значит пароль доехал до песочницы раскрытым каналом tool_args.
"""

from __future__ import annotations

from typing import Any

import pytest
from web_sandbox import (
    Credentials,
    needs_sandbox,
    needs_userns,
    sandbox_profile,
)

from boba.sandbox import SandboxProfile
from boba.sandbox.caller import SandboxCaller, SandboxPayloadError
from boba.sandbox.workflow import StageDef, StageRegistry
from boba.tool.web._fetch import web_fetch
from boba.tool.web._grep import WebGrepConfig, web_grep
from boba.tool.web.stages import WebStages
from boba.toolkit.launcher import LauncherFactory, PayloadFailureError, ToolLauncher
from boba.web.caller import WebCaller
from boba.web.protocol import (
    WebFetchRequest,
    WebGrepRequest,
    WebNodes,
    WebOp,
    WebProfile,
)


def _allow_all(tool: str, /) -> bool:
    return True


def _config(origin: str, *, guarded: bool) -> WebGrepConfig:
    auth: dict[str, str] = {"method": "none"}
    if guarded:
        auth = {
            "method": "basic",
            "user": Credentials.USER,
            "password": Credentials.PASSWORD,
        }

    profile: dict[str, Any] = {
        "timeout_sec": 30.0,
        "ssl_verify": False,
        "auth": auth,
    }

    return WebGrepConfig.model_validate(
        {"profiles": {"127.0.0.1": profile}, "max_text_chars": 500}
    )


def _launchers(cfg: WebGrepConfig) -> LauncherFactory:
    stages = WebStages(cfg)
    profile = SandboxProfile.model_validate(sandbox_profile())

    defs = {
        WebOp.FETCH.value: StageDef(
            contract=WebNodes.FETCH,
            profile=profile,
            entry=WebNodes.ENTRY,
            request=WebFetchRequest,
            enrich=stages.fetch,
        ),
        WebOp.GREP.value: StageDef(
            contract=WebNodes.GREP,
            profile=profile,
            entry=WebNodes.ENTRY,
            request=WebGrepRequest,
            enrich=stages.grep,
        ),
    }

    caller = SandboxCaller(StageRegistry(defs), _allow_all, dict)

    def factory(tool: str, /) -> ToolLauncher:
        return caller

    return factory


def _caller(cfg: WebGrepConfig) -> WebCaller:
    return WebCaller("web", _launchers(cfg))


@needs_sandbox
@needs_userns
class TestWebNodesInSandbox:
    """Продукт узла течёт каналом данных, счётчики — квитанцией."""

    def test_fetch_returns_the_window(self, http_origin: str) -> None:
        cfg = _config(http_origin, guarded=False)

        answer = web_fetch(
            cfg,
            _caller(cfg),
            url=f"{http_origin}/page",
            as_markdown=True,
            line_offset=0,
            line_count=100,
        )

        assert "Отчёт" in answer.content
        assert answer.total_lines > 0
        assert answer.returned_lines == answer.total_lines

    def test_fetch_window_is_paginated(self, http_origin: str) -> None:
        cfg = _config(http_origin, guarded=False)
        caller = _caller(cfg)

        whole = web_fetch(
            cfg,
            caller,
            url=f"{http_origin}/page",
            as_markdown=True,
            line_offset=0,
            line_count=100,
        )
        tail = web_fetch(
            cfg,
            caller,
            url=f"{http_origin}/page",
            as_markdown=True,
            line_offset=1,
            line_count=1,
        )

        assert tail.returned_lines == 1
        assert tail.total_lines == whole.total_lines

    def test_grep_returns_matches_as_rows(self, http_origin: str) -> None:
        cfg = _config(http_origin, guarded=False)

        result = web_grep(
            cfg,
            _caller(cfg),
            url=f"{http_origin}/page",
            pattern="искомая",
            as_markdown=True,
            case_insensitive=False,
            context=1,
            limit=10,
            fixed_string=False,
        )

        assert len(result.rows) == 1
        row = result.rows[0]
        assert "метка искомая" in row["content"]
        assert len(row["after"]) == 1

    def test_basic_auth_secret_reaches_the_sandbox(self, http_origin: str) -> None:
        """Страница под паролем: без раскрытых кредов сервер ответил бы 401."""
        cfg = _config(http_origin, guarded=True)

        answer = web_fetch(
            cfg,
            _caller(cfg),
            url=f"{http_origin}/private",
            as_markdown=False,
            line_offset=0,
            line_count=10,
        )

        assert "Отчёт" in answer.content

    def test_missing_page_is_an_expected_failure(self, http_origin: str) -> None:
        cfg = _config(http_origin, guarded=False)

        with pytest.raises(PayloadFailureError) as failure:
            web_fetch(
                cfg,
                _caller(cfg),
                url=f"{http_origin}/ghost",
                as_markdown=False,
                line_offset=0,
                line_count=10,
            )

        assert failure.value.kind == "web_request_failed"

    def test_host_outside_the_whitelist_is_rejected(self, http_origin: str) -> None:
        cfg = _config(http_origin, guarded=False)

        with pytest.raises(SandboxPayloadError, match="enrichment failed"):
            web_fetch(
                cfg,
                _caller(cfg),
                url="http://example.org/page",
                as_markdown=False,
                line_offset=0,
                line_count=10,
            )


class TestProfileSecrets:
    """Креды раскрываются только json-сериализацией запроса в tool_args."""

    @staticmethod
    def _profile() -> WebProfile:
        return WebProfile.model_validate(
            {
                "timeout_sec": 30.0,
                "ssl_verify": True,
                "auth": {
                    "method": "basic",
                    "user": Credentials.USER,
                    "password": Credentials.PASSWORD,
                },
            }
        )

    def test_json_dump_reveals_the_password(self) -> None:
        dumped = self._profile().model_dump_json()

        assert Credentials.PASSWORD in dumped

    def test_repr_and_python_dump_keep_it_masked(self) -> None:
        profile = self._profile()

        dumped = profile.model_dump()
        assert Credentials.PASSWORD not in repr(profile)
        assert Credentials.PASSWORD not in str(dumped)

    def test_args_of_the_node_carry_no_secret(self, http_origin: str) -> None:
        """В спеке графа секретов нет, а обогащённые args держат профиль моделью."""
        cfg = _config(http_origin, guarded=True)
        args = {
            "url": f"{http_origin}/private",
            "as_markdown": False,
            "line_offset": 0,
            "line_count": 10,
        }

        enriched = WebStages(cfg).fetch(args)

        request = WebFetchRequest.model_validate(enriched)

        assert Credentials.PASSWORD not in str(args)
        assert Credentials.PASSWORD not in str(enriched)
        assert Credentials.PASSWORD in request.model_dump_json()
