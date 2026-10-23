"""python -m boba.agent_service --config <toml> --site <toml>."""

import asyncio

from boba.agent_service.app import main

if __name__ == "__main__":
    asyncio.run(main())
