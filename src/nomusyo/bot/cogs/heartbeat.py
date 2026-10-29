"""Dead-man's-switch heartbeat: periodically GET ``BOT_HEARTBEAT_URL`` while the bot is healthy.

Loaded only when the URL is configured. The URL is a secret: never logged, and errors are
reported by type only (``requests`` exception messages can contain the URL).
"""

from __future__ import annotations

import asyncio
import logging

import requests
from discord.ext import commands, tasks

from nomusyo.bot.app import NomusyoBot

log = logging.getLogger(__name__)

HEARTBEAT_INTERVAL_MINUTES = 5
HEARTBEAT_TIMEOUT_SECONDS = 10


def send_ping(url: str, *, get=requests.get) -> bool:
    """Blocking GET. Returns True on a 2xx response. Never raises, never logs the URL."""
    try:
        response = get(url, timeout=HEARTBEAT_TIMEOUT_SECONDS, allow_redirects=False)
        ok = 200 <= response.status_code < 300
    except Exception as exc:  # noqa: BLE001
        log.warning("Heartbeat ping failed (%s)", type(exc).__name__)
        return False
    if not ok:
        log.warning("Heartbeat ping returned status %d", response.status_code)
    return ok


class HeartbeatCog(commands.Cog):
    def __init__(self, bot: NomusyoBot) -> None:
        self.bot = bot
        self._url = bot.config.heartbeat_url
        # discord.py keeps is_ready() True while reconnecting, so track the gateway state here.
        self._connected = False

    async def cog_load(self) -> None:
        self.ping_loop.start()

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        self._connected = True

    @commands.Cog.listener()
    async def on_resumed(self) -> None:
        self._connected = True

    @commands.Cog.listener()
    async def on_disconnect(self) -> None:
        self._connected = False

    async def cog_unload(self) -> None:
        self.ping_loop.cancel()

    @tasks.loop(minutes=HEARTBEAT_INTERVAL_MINUTES)
    async def ping_loop(self) -> None:
        # Skip while disconnected so a dead gateway makes the external monitor fire.
        if self.bot.is_closed() or not self._connected:
            return
        await asyncio.to_thread(send_ping, self._url)

    @ping_loop.before_loop
    async def _before_ping(self) -> None:
        await self.bot.wait_until_ready()


async def setup(bot: NomusyoBot) -> None:
    await bot.add_cog(HeartbeatCog(bot))
