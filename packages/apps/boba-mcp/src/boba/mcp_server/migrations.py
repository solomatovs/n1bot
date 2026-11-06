"""Получение SQL-планов плагинов для команды migrate MCP.

Ошибки:
ConfigError — план или конфигурация подключения не прошли проверку.
RuntimeError — программа плагина не смогла предоставить план."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from pathlib import Path

from omegaconf import DictConfig, OmegaConf
from pydantic import ValidationError

from boba.config import ConfigError, ConfigSchema, bind
from boba.core.validation import ValidationText
from boba.db.postgres.connection import PostgresConfig
from boba.db.postgres.migrate import MigrationPlan, MigrationTarget
from boba.db.postgres.migration_cli import ApplicationMigrations, MigrationSources
from boba.krb.layout import InstanceLayout
from boba.mcp_server.config.models import DataConfig, McpAppConfig, TlsConfig
from boba.toolrun.launchers import PluginLaunch, ToolLaunchers
from boba.toolrun.plugins import PluginSections
from boba.toolrun.sandbox import StandardToolLaunchers


def configured_migrations() -> ApplicationMigrations:
    """Compose MCP's SQL plan and enabled plugin plans for one migrate run."""
    return ApplicationMigrations(
        (PluginMigrations(),),
        schema=ConfigSchema(McpAppConfig, values=("auth",)),
        prepare=prepare_migrations,
    )


class PluginMigrations(MigrationSources):
    """Читает SQL-планы включённых плагинов для migrate MCP и тестовых баз.

    Реализация MigrationSources: программа плагина отдаёт migration-plan
    с SQL выбранной версии. ApplicationMigrations исполняет его тем же
    механизмом, что план MCP. Boba и AID этот источник
    не подключают.
    """

    async def targets(self, raw: DictConfig) -> Sequence[MigrationTarget]:
        targets: list[MigrationTarget] = []
        sections = PluginSections(raw)
        paths = bind(raw, "data", DataConfig)
        tls = bind(raw, "tls", TlsConfig)
        backends = StandardToolLaunchers(Path(paths.data_dir), Path(tls.ca_file))
        factory = ToolLaunchers(raw, backends.registry)
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


def prepare_migrations(raw: DictConfig, conf_dir: Path) -> None:
    """Подготовить окружение команды migrate из моделей приложения."""
    paths = bind(raw, "app.data", DataConfig)
    tls = bind(raw, "app.tls", TlsConfig)
    InstanceLayout(conf_dir, Path(paths.data_dir), Path(tls.ca_file)).apply()
