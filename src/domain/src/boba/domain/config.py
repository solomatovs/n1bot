"""Конфигурация приложения"""

from __future__ import annotations
from dataclasses import dataclass


@dataclass(frozen=True)
class AppConfig:
    """Единственный источник конфигурации приложения."""
    ssl_verify: bool = False
    log_level: str = "INFO"
