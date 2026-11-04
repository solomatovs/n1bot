"""Перенос старого каталога TOML в версионный поток MCP.

Ошибки:
ConfigError — файл импорта или его инструменты нельзя представить в новом каталоге.
PostgresError — SQL-план не собран; команда migrate завершается до изменений.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from omegaconf import DictConfig
from psycopg import sql
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from boba.config import ConfigError
from boba.connection_broker.tools import ConnectionTool
from boba.db.postgres.migrate import MigrationPlan, MigrationScript
from boba.mcp_server.catalog import McpEndpoint, PromptFile
from boba.runtime.launchers import ToolLaunchers
from boba.runtime.migrate import MigrationPreparation
from boba.runtime.plugins import PluginKits
from boba.toolrun.dev_null import DevNullTool
from boba.toolrun.stream_calls import WorkflowTool


class LegacyOffer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    roles: tuple[str, ...]
    offer: tuple[str, ...]


class LegacyEndpoint(BaseModel):
    """Однократный импорт старой секции endpoint'а, включая её промпты."""

    model_config = ConfigDict(extra="forbid")

    roles: tuple[str, ...]
    tools: tuple[str, ...]
    connections: tuple[LegacyOffer, ...] = ()
    prompts: dict[str, PromptFile] = Field(default_factory=dict)
    description: str = ""
    title: str = ""
    instructions: str = ""


class ImportedEndpoint(BaseModel):
    """Параметры данных миграции; roles разрешаются в UUID внутри её транзакции."""

    prefix_url: str
    title: str
    description: str
    instructions: str
    tools: tuple[str, ...]
    roles: tuple[str, ...]
    prompts: dict[str, PromptFile]


@dataclass(frozen=True)
class LegacyTools:
    names: frozenset[str]
    connected: frozenset[str]
    streaming: frozenset[str]


class McpMigrationInput(MigrationPreparation):
    """Собирает входные данные SQL-миграции из явного файла администратора.

    Вызывается ApplicationMigrations перед транзакциями. Сам базы не меняет;
    SQL сверяет прежние role_tools и сохраняет данные только один раз.
    """

    def __init__(self, path: Path | None) -> None:
        self._path = path
        self._parser = TypeAdapter(dict[str, LegacyEndpoint])

    async def prepare(self, raw: DictConfig, plan: MigrationPlan) -> MigrationPlan:
        entries: Mapping[str, LegacyEndpoint] = {}
        if self._path is not None:
            try:
                entries = self._parser.validate_json(self._path.read_bytes())
            except (OSError, ValidationError) as exc:
                raise ConfigError(
                    f"reading endpoint import {self._path} failed: {exc}"
                ) from exc

        imported: list[ImportedEndpoint] = []
        if entries:
            tools = await self._tool_names(raw)
            for name, entry in entries.items():
                imported.append(self._entry(name, entry, tools))

        values = [entry.model_dump(mode="json") for entry in imported]
        statement = sql.SQL(
            "select set_config('boba.mcp.endpoint_import', {}, true)"
        ).format(sql.Literal(json.dumps(values, ensure_ascii=False)))
        script = MigrationScript(
            name="endpoint import input", text=statement.as_string()
        )

        return plan.model_copy(update={"inputs": (script,)})

    async def _tool_names(self, raw: DictConfig) -> LegacyTools:
        launchers = ToolLaunchers(raw).build()
        try:
            kits = await PluginKits(raw, launchers).load()
        finally:
            launchers.stop()

        names = {str(name) for name in ConnectionTool}
        connected = set(names)
        streaming: set[str] = set()
        names.add("file_upload")
        for kit in kits.values():
            declarations = {tool.name: tool for tool in kit.launch.manifest.tools}
            unknown = set(kit.meta.tools) - declarations.keys()
            if unknown:
                raise ConfigError(
                    f"plugin {kit.launch.id!r}: unknown enabled tools {sorted(unknown)}"
                )

            for name in kit.meta.tools:
                names.add(name)
                if declarations[name].connections:
                    connected.add(name)

                if declarations[name].stream.streaming():
                    streaming.add(name)
                    names.add(WorkflowTool.NAME)

                if declarations[name].stream.outbound():
                    names.add(DevNullTool.NAME)

        return LegacyTools(frozenset(names), frozenset(connected), frozenset(streaming))

    def _entry(
        self, name: str, source: LegacyEndpoint, known: LegacyTools
    ) -> ImportedEndpoint:
        path = "/" + name.lstrip("/")
        title = source.title
        if not title:
            title = name

        self._offers(path, source)

        names = list(source.tools)
        if "*" in names:
            names = sorted(known.names)

        if "file_upload" not in names:
            names.append("file_upload")

        unknown = set(names) - known.names
        if unknown:
            raise ConfigError(
                f"endpoint {path}: imported tools are not loaded: {sorted(unknown)}"
            )

        if not source.connections:
            names = [name for name in names if name not in known.connected]

        if known.streaming.isdisjoint(names):
            names = [name for name in names if name != WorkflowTool.NAME]
        elif WorkflowTool.NAME not in names:
            names.append(WorkflowTool.NAME)

        try:
            McpEndpoint(
                id=UUID(int=0),
                prefix_url=path,
                title=title,
                tools=tuple(names),
                prompts=source.prompts,
            )
        except ValidationError as exc:
            raise ConfigError(f"endpoint import {path!r} is invalid: {exc}") from exc

        entry = ImportedEndpoint(
            prefix_url=path,
            title=title,
            description=source.description,
            instructions=source.instructions,
            tools=tuple(names),
            roles=source.roles,
            prompts=source.prompts,
        )
        return entry

    def _offers(self, path: str, source: LegacyEndpoint) -> None:
        for rule in source.connections:
            if set(rule.roles) != {"*"}:
                raise ConfigError(
                    f"endpoint {path}: role-specific connection offer rules need "
                    "separate endpoints before import"
                )

            if set(rule.offer) != {"stored"}:
                raise ConfigError(
                    f"endpoint {path}: expected only stored connection offers"
                )
