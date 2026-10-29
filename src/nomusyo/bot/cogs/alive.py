"""Container liveness file: touch ``ALIVE_FILE`` periodically while the gateway is ready.

The Docker HEALTHCHECK checks the file's mtime, so a hung or disconnected bot turns unhealthy.
"""

from __future__ import annotations

import logging
from pathlib import Path

from discord.ext import commands, tasks

from nomusyo.bot.app import NomusyoBot

log = logging.getLogger(__name__)

ALIVE_FILE = Path("/tmp/alive")
ALIVE_INTERVAL_SECONDS = 30


def touch_alive(path: Path = ALIVE_FILE) -> bool:
    try:
        path.touch()
    except OSError as exc:
        log.warning("Could not update alive file (%s)", type(exc).__name__)
        return False
    return True


class AliveCog(commands.Cog):
    def __init__(self, bot: NomusyoBot) -> None:
        self.bot = bot
        self.alive_loop.start()

    async def cog_unload(self) -> None:
        self.alive_loop.cancel()

    @tasks.loop(seconds=ALIVE_INTERVAL_SECONDS)
    async def alive_loop(self) -> None:
        if self.bot.is_closed() or not self.bot.is_ready():
            return
        touch_alive()

    @alive_loop.before_loop
    async def _before_alive(self) -> None:
        await self.bot.wait_until_ready()


async def setup(bot: NomusyoBot) -> None:
    await bot.add_cog(AliveCog(bot))
