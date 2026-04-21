"""FieldSpec'и секции ``[chainlit]``."""

from __future__ import annotations

from collections.abc import Mapping

from boba.domain.core.config import (
    ChainedConfigResolver,
    CsvListConverter,
    FieldSpec,
    StrConverter,
)
from boba.infra.config import default_resolver

__all__ = [
    "AUTH_SECRET",
    "HEADLESS",
    "HOST",
    "MODELS",
    "PORT",
    "ROOT_PATH",
    "chainlit_resolver",
    "load_models",
]


HOST = FieldSpec("CHAINLIT_HOST", StrConverter(), "127.0.0.1")
PORT = FieldSpec("CHAINLIT_PORT", StrConverter(), "8501")
ROOT_PATH = FieldSpec("CHAINLIT_ROOT_PATH", StrConverter(), "")
AUTH_SECRET = FieldSpec("CHAINLIT_AUTH_SECRET", StrConverter(), None)
HEADLESS = FieldSpec("CHAINLIT_HEADLESS", StrConverter(), "true")
MODELS = FieldSpec("CHAINLIT_MODELS", CsvListConverter(), [])


TOML_PATHS: Mapping[str, tuple[str, str]] = {
    "CHAINLIT_HOST": ("chainlit", "host"),
    "CHAINLIT_PORT": ("chainlit", "port"),
    "CHAINLIT_ROOT_PATH": ("chainlit", "root_path"),
    "CHAINLIT_AUTH_SECRET": ("chainlit", "auth_secret"),
    "CHAINLIT_HEADLESS": ("chainlit", "headless"),
    "CHAINLIT_MODELS": ("chainlit", "models"),
}


def chainlit_resolver() -> ChainedConfigResolver:
    return default_resolver(extra_toml_paths=TOML_PATHS)


def load_models() -> list[str]:
    return MODELS.read(chainlit_resolver())
