"""Адреса Confluence: REST-пути ↔ роли по таблице примеров, корень сервиса,
порт схемы по умолчанию, отказы разбора."""

from __future__ import annotations

import pytest

from boba.confluence.address import ConfluenceAddresses, ConfluenceNodeKind
from boba.connections.address import AddressError

BASE = {
    "scheme": "https",
    "host": "cwiki.apache.org",
    "port": 443,
    "root": "/confluence",
}

EXAMPLES: list[tuple[ConfluenceNodeKind, dict[str, object], str]] = [
    (
        ConfluenceNodeKind.SPACE,
        {"space": "FLINK"},
        "https://cwiki.apache.org/confluence/rest/api/space/FLINK",
    ),
    (
        ConfluenceNodeKind.PAGE,
        {"page": "307136992"},
        "https://cwiki.apache.org/confluence/rest/api/content/307136992",
    ),
    (
        ConfluenceNodeKind.ATTACHMENT,
        {"page": "307136992", "file": "design.pdf"},
        "https://cwiki.apache.org/confluence/download/attachments/307136992/design.pdf",
    ),
]


@pytest.mark.parametrize(("kind", "roles", "text"), EXAMPLES)
def test_examples_round_trip(
    kind: ConfluenceNodeKind, roles: dict[str, object], text: str
) -> None:
    address = ConfluenceAddresses.parse(kind, text)

    assert kind == type(address).KIND
    assert address.to_json() == {**BASE, **roles}
    assert address.render() == text

    assert ConfluenceAddresses.parse_any(text) == address


def test_explicit_default_port_is_dropped_from_the_string() -> None:
    address = ConfluenceAddresses.parse(
        ConfluenceNodeKind.SPACE,
        "https://cwiki.apache.org:443/confluence/rest/api/space/FLINK",
    )

    assert address.to_json()["port"] == 443
    assert (
        address.render() == "https://cwiki.apache.org/confluence/rest/api/space/FLINK"
    )


def test_custom_port_and_http_are_kept() -> None:
    text = "http://wiki.corp:8090/rest/api/content/42"

    address = ConfluenceAddresses.parse(ConfluenceNodeKind.PAGE, text)

    assert address.to_json() == {
        "scheme": "http",
        "host": "wiki.corp",
        "port": 8090,
        "root": "",
        "page": "42",
    }
    assert address.render() == text


def test_file_name_with_space_is_quoted() -> None:
    text = "https://wiki.corp/download/attachments/42/design%20v2.pdf"

    address = ConfluenceAddresses.parse(ConfluenceNodeKind.ATTACHMENT, text)

    assert address.to_json()["file"] == "design v2.pdf"
    assert address.render() == text


def test_kind_must_match_path() -> None:
    with pytest.raises(AddressError, match="matches none of its shapes"):
        ConfluenceAddresses.parse(
            ConfluenceNodeKind.PAGE,
            "https://cwiki.apache.org/confluence/rest/api/space/FLINK",
        )


@pytest.mark.parametrize(
    "text",
    [
        "https://user:pw@cwiki.apache.org/confluence/rest/api/space/FLINK",
        "https://cwiki.apache.org/confluence/rest/api/space/FLINK?expand=description",
        "https://cwiki.apache.org/confluence/rest/api/space/FLINK#top",
        "https://cwiki.apache.org/confluence/pages/viewpage.action?pageId=1",
        "ftp://cwiki.apache.org/confluence/rest/api/space/FLINK",
    ],
)
def test_malformed_url_is_refused(text: str) -> None:
    with pytest.raises(AddressError):
        ConfluenceAddresses.parse(ConfluenceNodeKind.SPACE, text)


def test_prompt_lists_every_kind() -> None:
    prompt = ConfluenceAddresses.prompt()

    for kind in ConfluenceNodeKind:
        assert f"{kind}:" in prompt
