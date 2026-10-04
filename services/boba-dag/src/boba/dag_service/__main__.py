"""python -m boba.dag_service --config <toml>."""

import asyncio

from boba.dag_service.app import main

if __name__ == "__main__":
    asyncio.run(main())
