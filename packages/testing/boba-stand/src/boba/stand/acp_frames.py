"""Кадры соединения ACP v2 в тестах и их сверка со схемой ACP.

Данные: `acp_schema/` — схема ACP v2, тег schema-v2.0.0-alpha.8.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, ClassVar

from acp.connection import StreamDirection, StreamEvent
from jsonschema import Draft202012Validator


class Frames:
    """Наблюдатель кадров соединения ACP SDK: копит кадры обоих направлений
    в порядке провода и сверяет их со схемой ACP v2.

    Экземпляр передаётся соединению в `observers`; им пользуются тесты пакета
    boba-transport-acp (пара агент — клиент в памяти) и тесты boba-cli
    (клиент поверх stdio настоящего `boba --stdio`). Сверка идёт после конца
    обмена: каждый кадр — с корневой схемой, его params или result — ещё и с
    определением метода по x-method.
    """

    SCHEMA: ClassVar[Path] = Path(__file__).parent / "acp_schema/schema.unstable.json"

    def __init__(self) -> None:
        self.events: list[StreamEvent] = []
        self._root: dict[str, Any] = json.loads(self.SCHEMA.read_text(encoding="utf-8"))
        self._schema = Draft202012Validator(self._root)

    def __call__(self, event: StreamEvent) -> None:
        self.events.append(event)

    def request_id(self, method: str, index: int = 0) -> int:
        found: list[int] = []
        for event in self.events:
            if event.direction is not StreamDirection.OUTGOING:
                continue

            message = event.message
            if message.get("method") == method and "id" in message:
                found.append(int(message["id"]))

        return found[index]

    def position_of_response(self, request_id: int) -> int:
        for position, event in enumerate(self.events):
            if event.direction is not StreamDirection.INCOMING:
                continue

            message = event.message
            if "method" in message:
                continue

            if message.get("id") == request_id:
                return position

        msg = f"no response to request {request_id} seen"
        raise AssertionError(msg)

    def incoming_notifications(self, before: int) -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = []
        for event in self.events[:before]:
            if event.direction is not StreamDirection.INCOMING:
                continue

            if "method" in event.message and "id" not in event.message:
                found.append(event.message)

        return found

    def incoming_after(self, position: int) -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = []
        for event in self.events[position + 1 :]:
            if event.direction is StreamDirection.INCOMING:
                found.append(event.message)

        return found

    def check_schema(self) -> None:
        """Сверяет все накопленные кадры со схемой ACP v2."""
        defs = self._root["$defs"]
        answered = {
            StreamDirection.INCOMING: StreamDirection.OUTGOING,
            StreamDirection.OUTGOING: StreamDirection.INCOMING,
        }
        asked: dict[tuple[StreamDirection, object], str] = {}

        for frame in self.events:
            message = frame.message
            self._schema.validate(message)

            if "method" not in message:
                method = asked.pop((answered[frame.direction], message["id"]))
                part = "Response"
                payload = message.get("result")
            elif "id" in message:
                method = message["method"]
                asked[(frame.direction, message["id"])] = method
                part = "Request"
                payload = message["params"]
            else:
                method = message["method"]
                part = "Notification"
                payload = message["params"]

            if payload is None:
                continue

            # корневая схема пускает любые params через Ext*, поэтому сверяем ещё и
            # с определением метода по x-method
            names = [
                name
                for name, body in defs.items()
                if body.get("x-method") == method and name.endswith(part)
            ]

            if len(names) != 1:
                msg = (
                    f"ACP schema: expected one {part} definition with x-method "
                    f"{method!r}, found {names}"
                )
                raise AssertionError(msg)

            frame_schema = {"$ref": f"#/$defs/{names[0]}", "$defs": defs}
            self._schema.evolve(schema=frame_schema).validate(payload)
