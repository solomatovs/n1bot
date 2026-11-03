"""Подготовка, применение и откат явного сопоставления users с личностями.

Ошибки:
SystemExit — конфиг, соответствия или план не приняты, файл недоступен
    либо хранилище отказало; рабочие сервисы не запускаются.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections.abc import Sequence
from enum import StrEnum
from pathlib import Path
from typing import Annotated, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from boba.chat.threads import DataUnavailableError
from boba.config import ConfigError, ConfigLoader, bind
from boba.core.validation import ValidationText
from boba.db.postgres import AsyncPostgresPool, PostgresError
from boba.db.postgres.connection import PostgresConfig
from boba.identity.api import (
    UserIdentityMigration,
    UserIdentitySnapshot,
)
from boba.runtime.config import InstanceConfig
from boba.runtime.users import UsersTable


class IdentityCommand(StrEnum):
    """Операции над планом идентификаторов пользователей."""

    PLAN = "plan"
    APPLY = "apply"
    ROLLBACK = "rollback"


class IdentityCall(BaseModel):
    """Общие аргументы административного входа: файлы конфигурации хранилища."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    configs: tuple[Path, ...] = Field(min_length=1)


class IdentityPlan(IdentityCall):
    """Явные соответствия и новый файл плана без изменения данных users."""

    command: Literal[IdentityCommand.PLAN]
    mapping: Path
    plan: Path


class IdentityApply(IdentityCall):
    """Применение или откат ранее проверенного плана."""

    command: Literal[IdentityCommand.APPLY, IdentityCommand.ROLLBACK]
    plan: Path


IdentityOperation = Annotated[
    IdentityPlan | IdentityApply, Field(discriminator="command")
]


class IdentityStorageConfig(BaseModel):
    """Подключение и схема users из [app], без запуска остальных компонентов."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    postgres: PostgresConfig
    instance: InstanceConfig
    db_schema: str = Field(alias="schema", min_length=1)


class IdentityPlans:
    """Файлы планов с исходными metadata: только владелец читает новый файл."""

    ENCODING: ClassVar[str] = "utf-8"

    def read(self, path: Path) -> UserIdentitySnapshot:
        return UserIdentitySnapshot.model_validate_json(
            path.read_text(encoding=self.ENCODING)
        )

    def write(self, path: Path, snapshot: UserIdentitySnapshot) -> None:
        document = snapshot.model_dump_json(indent=2)
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding=self.ENCODING) as output:
            output.write(document)


class IdentityMigrationEntry:
    """Административный CLI над общими UsersTable и файлами планов IdentityPlans."""

    def __init__(self) -> None:
        self._parser = argparse.ArgumentParser(
            description="Map user identities while retaining users UUIDs and grants"
        )
        self._parser.add_argument(
            "--config", dest="configs", action="append", type=Path, required=True
        )
        commands = self._parser.add_subparsers(dest="command", required=True)
        plan = commands.add_parser(IdentityCommand.PLAN.value)
        plan.add_argument("mapping", type=Path)
        plan.add_argument("plan", type=Path)
        for command in (IdentityCommand.APPLY, IdentityCommand.ROLLBACK):
            change = commands.add_parser(command.value)
            change.add_argument("plan", type=Path)

        self._calls = TypeAdapter[IdentityOperation](IdentityOperation)
        self._plans = IdentityPlans()

    @staticmethod
    def _output(message: str) -> None:
        sys.stdout.write(f"{message}\n")

    async def run(self, arguments: Sequence[str] | None = None) -> None:
        call = self._calls.validate_python(vars(self._parser.parse_args(arguments)))
        raw = ConfigLoader(call.configs).load()
        config = bind(raw, "app", IdentityStorageConfig)
        config.instance.layout(call.configs[0].resolve().parent).apply()
        pool = await AsyncPostgresPool.get(config.postgres)
        try:
            users = UsersTable(config.postgres, config.db_schema, pool)
            if isinstance(call, IdentityPlan):
                mapping = bind(
                    ConfigLoader([call.mapping]).load(),
                    "migration",
                    UserIdentityMigration,
                )
                snapshot = await users.identity_snapshot(mapping)
                self._plans.write(call.plan, snapshot)
                for change in snapshot.changes:
                    previous = "new user"
                    if change.before is not None:
                        previous = change.before.identifier

                    self._output(
                        f"{change.after.id}: {previous} -> {change.after.identifier}"
                    )

                self._output(f"Plan saved to {call.plan}; users data is unchanged")
                return

            snapshot = self._plans.read(call.plan)
            await users.apply_identity_snapshot(
                snapshot, rollback=call.command is IdentityCommand.ROLLBACK
            )
            self._output(
                f"{call.command.value}: {len(snapshot.changes)} users "
                f"in {config.db_schema}"
            )
        finally:
            await pool.close()


async def main(arguments: Sequence[str] | None = None) -> None:
    try:
        await IdentityMigrationEntry().run(arguments)
    except ValidationError as exc:
        raise SystemExit(f"user identity migration: {ValidationText.of(exc)}") from exc
    except ConfigError as exc:
        reason = str(exc)
        if isinstance(exc.__cause__, ValidationError):
            reason = ValidationText.of(exc.__cause__)

        raise SystemExit(f"user identity migration: {reason}") from exc
    except (DataUnavailableError, PostgresError, OSError, ValueError) as exc:
        raise SystemExit(f"user identity migration: {exc}") from exc


def cli() -> None:
    asyncio.run(main())


if __name__ == "__main__":
    cli()
