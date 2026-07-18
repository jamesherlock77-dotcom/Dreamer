import asyncio
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Optional

import aiohttp
import discord
from discord.ext import commands, tasks

# ---------------------------------------------------------------------------
# Configuration (set these as environment variables, don't hardcode secrets)
# ---------------------------------------------------------------------------
DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]
CHANNEL_ID = int(os.environ["DISCORD_CHANNEL_ID"])

OCULUS_ACCESS_TOKEN = os.environ.get("OCULUS_ACCESS_TOKEN", "OC|752908224809889|")
APP_ID = int(os.environ.get("OCULUS_APP_ID", "7190422614401072"))
DOCID = int(os.environ.get("OCULUS_DOCID", "6771539532935162"))

CHECK_INTERVAL_MINUTES = float(os.environ.get("CHECK_INTERVAL_MINUTES", "10"))
STATE_FILE = Path(__file__).parent / "state.json"

# Branding / display config
BOT_BRAND_NAME = os.environ.get("BOT_BRAND_NAME", "Kirbs - Tracker")
GAME_PUBLISHER = os.environ.get("GAME_PUBLISHER", "Wooster Games")
GAME_NAME = os.environ.get("GAME_NAME", "Animal Company")
GAME_STORE_URL = os.environ.get(
    "GAME_STORE_URL", f"https://www.meta.com/en-us/experiences/{APP_ID}/"
)
EMBED_COLOR = discord.Color.dark_grey()

# ---------------------------------------------------------------------------
# GraphQL client (from your original script, unchanged logic)
# ---------------------------------------------------------------------------
class GraphQLClient:
    def __init__(
        self,
        url: str = "https://graph.oculus.com/graphql",
        max_requests: int = 5,
        per_seconds: float = 5.0,
    ) -> None:
        self.url = url
        self.max_requests = max_requests
        self.per_seconds = per_seconds
        self._timestamps: list[float] = []
        self._session: Optional[aiohttp.ClientSession] = None
        self._timeout = aiohttp.ClientTimeout(total=15)

    async def _acquire_slot(self) -> None:
        now = asyncio.get_running_loop().time()
        self._timestamps = [t for t in self._timestamps if now - t < self.per_seconds]

        if len(self._timestamps) >= self.max_requests:
            delay = self.per_seconds - (now - self._timestamps[0])
            if delay > 0:
                await asyncio.sleep(delay)

        self._timestamps.append(asyncio.get_running_loop().time())

    async def post(self, payload: dict) -> Optional[dict]:
        await self._acquire_slot()

        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self._timeout)

        try:
            async with self._session.post(self.url, data=payload) as resp:
                resp.raise_for_status()
                return await resp.json(content_type=None)
        except Exception as e:
            print(f"GraphQL error: {type(e).__name__}: {e}")

            if self._session and not self._session.closed:
                await self._session.close()
            self._session = None
            return None

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()


graphql_client = GraphQLClient()


def _payload() -> dict:
    return {
        "access_token": OCULUS_ACCESS_TOKEN,
        "variables": json.dumps({"applicationID": str(APP_ID)}),
        "doc_id": str(DOCID),
    }


async def fetch_store_metadata() -> Optional[dict]:
    data = await graphql_client.post(_payload())
    return data if isinstance(data, dict) else None


def _extract_live_version(meta: dict) -> Optional[str]:
    nodes = meta.get("data", {}).get("node", {}).get("liveChannel", {}).get("nodes", [])
    if not nodes:
        return None
    return nodes[0].get("latest_supported_binary", {}).get("version")


def _extract_dev_version(meta: dict) -> Optional[str]:
    nodes = meta.get("data", {}).get("node", {}).get("primary_binaries", {}).get("nodes", [])
    if not nodes:
        return None
    return nodes[0].get("version")


async def get_versions() -> tuple[Optional[str], Optional[str]]:
    """Returns (live_version, dev_version)."""
    meta = await fetch_store_metadata()
    if not isinstance(meta, dict):
        return None, None
    return _extract_live_version(meta), _extract_dev_version(meta)


# ---------------------------------------------------------------------------
# Game banner image (pulled from the store page's Open Graph image)
# ---------------------------------------------------------------------------
_OG_IMAGE_RE = re.compile(
    r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']',
    re.IGNORECASE,
)

_banner_cache: dict[str, Any] = {"url": None, "fetched_at": 0.0}
_BANNER_CACHE_TTL = 60 * 60  # 1 hour


async def fetch_game_banner_url() -> Optional[str]:
    """Scrapes the game's store page for its og:image (banner/cover art)."""
    now = time.time()
    if _banner_cache["url"] and (now - _banner_cache["fetched_at"] < _BANNER_CACHE_TTL):
        return _banner_cache["url"]

    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=10),
            headers={"User-Agent": "Mozilla/5.0 (compatible; VersionTrackerBot/1.0)"},
        ) as session:
            async with session.get(GAME_STORE_URL) as resp:
                if resp.status != 200:
                    print(f"Banner fetch failed: HTTP {resp.status}")
                    return _banner_cache["url"]
                html = await resp.text()
    except Exception as e:
        print(f"Banner fetch error: {type(e).__name__}: {e}")
        return _banner_cache["url"]

    match = _OG_IMAGE_RE.search(html)
    if not match:
        print("Banner fetch: no og:image tag found on store page.")
        return _banner_cache["url"]

    url = match.group(1)
    _banner_cache["url"] = url
    _banner_cache["fetched_at"] = now
    return url


# ---------------------------------------------------------------------------
# Simple persisted state so we don't re-announce after a restart
# ---------------------------------------------------------------------------
def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {"live_version": None, "dev_version": None}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state))


def build_update_embed(
    old_version: Optional[str],
    new_version: Optional[str],
    banner_url: Optional[str],
) -> discord.Embed:
    now_ts = int(time.time())

    embed = discord.Embed(
        title="Update Detected!",
        description=(
            f"<t:{now_ts}:F> (<t:{now_ts}:R>)\n"
            f"**{GAME_PUBLISHER}, {GAME_NAME}**"
        ),
        color=EMBED_COLOR,
    )
    embed.set_author(name=BOT_BRAND_NAME)
    embed.add_field(
        name="🟢 | Updated Version:",
        value=f"```{new_version or 'unknown'}```",
        inline=False,
    )
    embed.add_field(
        name="🔴 | Last Logged:",
        value=f"```{old_version or 'none'}```",
        inline=False,
    )
    if banner_url:
        embed.set_image(url=banner_url)

    return embed


# ---------------------------------------------------------------------------
# Discord bot
# ---------------------------------------------------------------------------
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (id: {bot.user.id})")
    if not version_check_loop.is_running():
        version_check_loop.start()


@bot.command(name="testupdate")
async def testupdate(ctx: commands.Context):
    """Posts a sample update embed immediately, using real current data if available."""
    await ctx.send("🔧 Test message — the bot is alive and listening.")

    live, dev = await get_versions()
    banner_url = await fetch_game_banner_url()

    if live is None and dev is None:
        await ctx.send("⚠️ Couldn't fetch version info right now — check the access token/app ID.")
        # Still show what a formatted embed looks like, with placeholder versions.
        embed = build_update_embed("1.82.1.3202", "1.82.2.3211", banner_url)
        await ctx.send(embed=embed)
        return

    state = load_state()
    embed = build_update_embed(state.get("live_version"), live, banner_url)
    await ctx.send(embed=embed)


@bot.command(name="checknow")
async def checknow(ctx: commands.Context):
    """Manually triggers an update check right now (posts only if something changed)."""
    await check_for_updates(force_channel=ctx.channel)


async def check_for_updates(force_channel: Optional[discord.abc.Messageable] = None) -> None:
    state = load_state()
    live, dev = await get_versions()

    if live is None and dev is None:
        print("Version check failed, skipping this cycle.")
        if force_channel is not None:
            await force_channel.send("⚠️ Couldn't fetch version info right now.")
        return

    embeds_to_send = []

    if live is not None and live != state.get("live_version"):
        banner_url = await fetch_game_banner_url()
        embeds_to_send.append(build_update_embed(state.get("live_version"), live, banner_url))
        state["live_version"] = live

    if dev is not None and dev != state.get("dev_version"):
        banner_url = await fetch_game_banner_url()
        embeds_to_send.append(build_update_embed(state.get("dev_version"), dev, banner_url))
        state["dev_version"] = dev

    if embeds_to_send:
        save_state(state)
        channel = force_channel or bot.get_channel(CHANNEL_ID)
        if channel is not None:
            for embed in embeds_to_send:
                await channel.send(embed=embed)
    elif force_channel is not None:
        await force_channel.send("No changes since the last check.")


@tasks.loop(minutes=CHECK_INTERVAL_MINUTES)
async def version_check_loop():
    await check_for_updates()


@version_check_loop.before_loop
async def before_loop():
    await bot.wait_until_ready()


@bot.event
async def on_disconnect():
    await graphql_client.close()


if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
