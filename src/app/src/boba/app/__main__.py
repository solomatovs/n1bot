"""Точка входа: python -m boba.app"""

from __future__ import annotations

import logging

from boba.app.logging import configure_logging
from boba.infra.config import ConfigLoader
from boba.infra.container import create_container

logger = logging.getLogger(__name__)


def main() -> None:
    config = ConfigLoader().load()
    configure_logging(config.log_level)

    logger.info("starting boba")
    logger.debug("config loaded: log_level=%s", config.log_level)

    _container = create_container(config)

    logger.info("container ready, awaiting work…")


if __name__ == "__main__":
    main()
