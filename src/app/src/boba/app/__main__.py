"""Точка входа: python -m boba.app"""

from __future__ import annotations

import logging

from boba.infra.config import ConfigLoader

logger = logging.getLogger(__name__)


def main() -> None:
    loader = ConfigLoader()
    _ = loader.load_app()
    _ = loader.load_agent()

if __name__ == "__main__":
    main()
