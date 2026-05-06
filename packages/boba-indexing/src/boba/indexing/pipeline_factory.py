"""PipelineSpec: декларативная (section, builder) фабрика IndexPipeline."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from boba.config.app import AppConfig
from boba.config.section import ConfigSection
from boba.indexing.pipeline import IndexPipeline

__all__ = ["PipelineSpec"]


@dataclass(frozen=True)
class PipelineSpec:
    """Декларативная фабрика IndexPipeline: секция конфига + сборщик из AppConfig."""

    section: ConfigSection[Any]
    build: Callable[[AppConfig], IndexPipeline[Any]]
