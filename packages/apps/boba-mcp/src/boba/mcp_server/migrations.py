"""Получение SQL-планов плагинов для команды migrate MCP.

Ошибки:
ConfigError — план или конфигурация подключения не прошли проверку.
RuntimeError — программа плагина не смогла предоставить план."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence

from omegaconf import DictConfig, OmegaConf
from pydantic import BaseModel, ValidationError

from boba.config import ConfigError, ConfigSchema, bind
from boba.core.components import Components
from boba.core.validation import ValidationText
from boba.db.postgres.connection import PostgresConfig
from boba.db.postgres.migrate import MigrationPlan, MigrationTarget
from boba.db.postgres.migration_cli import ApplicationMigrations, MigrationSources
from boba.mcp_server.app import McpAppConfig
from boba.toolrun.launchers import PluginLaunch, SectionLaunchers, ToolLaunchers
from boba.toolrun.plugins import PluginSections


def configured_migrations() -> ApplicationMigrations:
    """Compose MCP's SQL plan and enabled plugin plans for one migrate run."""
    return ApplicationMigrations(
        (PluginMigrations(McpAppConfig.LAUNCHER_BACKENDS),),
        schema=ConfigSchema(McpAppConfig, values=("auth",)),
    )


class PluginMigrations(MigrationSources):
    """Читает SQL-планы включённых плагинов для migrate MCP и тестовых баз.

    Реализация MigrationSources: программа плагина отдаёт migration-plan
    с SQL выбранной версии. ApplicationMigrations исполняет его тем же
    механизмом, что план MCP. Boba и AID этот источник
    не подключают.
    """

    def __init__(
        self, backends: Components[BaseModel, DictConfig, SectionLaunchers]
    ) -> None:
        self._backends = backends

    async def targets(self, raw: DictConfig) -> Sequence[MigrationTarget]:
        targets: list[MigrationTarget] = []
        sections = PluginSections(raw)
        factory = ToolLaunchers(raw, self._backends)
        launchers = factory.build()
        try:
            launchers.probe()
            for plugin in sections.ids():
                if not sections.meta(plugin).enable:
                    continue

                values = sections.values(plugin)
                manifest = await asyncio.to_thread(
                    launchers.manifest_of, plugin, values
                )
                if not manifest.setup:
                    continue

                launch = PluginLaunch(manifest, values)
                document = await asyncio.to_thread(
                    launchers.migration_of,
                    launch,
                    values,
                    sections.meta(plugin).migration_version,
                )
                try:
                    storage = MigrationPlan.model_validate_json(document)
                except ValidationError as exc:
                    reason = ValidationText.of(exc)
                    msg = f"plugin {plugin}: invalid migration plan: {reason}"
                    raise ConfigError(msg) from exc

                connection = bind(
                    OmegaConf.create(values), storage.connection, PostgresConfig
                )
                targets.append(MigrationTarget(connection, storage))
        finally:
            launchers.stop()
        return targets
