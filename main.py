import discord
from discord.ext import commands, tasks
from discord import app_commands
import os
import re
import json
import aiohttp
from datetime import datetime, time
import pytz
from typing import Literal

# ── Config ───────────────────────────────────────────────────────────────────
TOKEN              = os.environ["DISCORD_BOT_TOKEN"]
YOUTUBE_API_KEY    = "AIzaSyAe5hyEAwxTCdBbZRQQsGfuQC6xlQWUBg0"
TIKAPI_KEY         = "lFoJPYkghHxXrc33873j9AOlf9RNV9XNw7hDek9xhH0w00q8"
DB_CHANNEL_ID      = 1515064641246466113
LINK_CMD_CHANNEL   = 1513272619439226980
LINK_LOG_CHANNEL   = 1512899799077093546
RESET_TIME         = time(23, 0)
RESET_WEEKDAY      = 6
TIMEZONE           = pytz.timezone("UTC")

# ── Streak config ────────────────────────────────────────────────────────────
STREAK_DB_CHANNEL       = 1515834119727222834
STREAK_ANNOUNCE_CHANNEL = 1423121104675016768
STREAK_ROLES = {
    1:   1495573627217641604,
    3:   1495573632984813639,
    7:   1495573635459448842,
    14:  1495573637800136844,
    30:  1495573640132034670,
    60:  1495573754921877687,
    100: 1495573763004039380,
}
MESSAGES_REQUIRED   = 3   # messages needed to earn/continue streak
STREAK_WINDOW_HOURS = 24  # hours after which streak window opens
STREAK_GRACE_HOURS  = 4   # hours the window stays open

# ── Cache ────────────────────────────────────────────────────────────────────
_approved_links_cache: dict | None = None
_approved_links_cache_time: float  = 0
CACHE_TTL = 300  # seconds (5 minutes)

# ── Bot setup ─────────────────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

# ── Log every message into the DB channel ────────────────────────────────────
@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return
    if message.channel.id in (DB_CHANNEL_ID, 1500327292830875898, STREAK_DB_CHANNEL):
        return

    db_channel = bot.get_channel(DB_CHANNEL_ID)
    if db_channel:
        await db_channel.send(f"{message.author.id}")

    if message.channel.id == 1440105578839146517:
        has_image = any(
            a.content_type and a.content_type.startswith("image/")
            for a in message.attachments
        )
        if not has_image:
            await message.delete()
            return

    await handle_streak(message)
    await bot.process_commands(message)

# ── Helper: tally message counts ─────────────────────────────────────────────
async def tally_counts() -> dict:
    db_channel = bot.get_channel(DB_CHANNEL_ID)
    counts = {}
    async for msg in db_channel.history(limit=None, oldest_first=True):
        if msg.author.bot:
            uid = msg.content.strip()
            if uid.isdigit():
                counts[uid] = counts.get(uid, 0) + 1
    return counts

# ── Helper: build approved links cache ───────────────────────────────────────
async def _build_links_cache() -> dict:
    """Scan LINK_LOG_CHANNEL once and cache results. Returns {uid: {platform, url}}."""
    import time as _time
    global _approved_links_cache, _approved_links_cache_time

    now = _time.monotonic()
    if _approved_links_cache is not None and (now - _approved_links_cache_time) < CACHE_TTL:
        return _approved_links_cache

    log_channel = bot.get_channel(LINK_LOG_CHANNEL)
    seen_users  = {}
    async for msg in log_channel.history(limit=None, oldest_first=True):
        if not msg.embeds:
            continue
        embed = msg.embeds[0]
        if not (embed.footer and embed.footer.text):
            continue
        footer    = embed.footer.text
        uid_match = re.search(r"User ID:\s*(\d+)", footer)
        if not uid_match:
            continue
        uid      = uid_match.group(1)
        platform = None
        url      = None
        for field in embed.fields:
            if field.name == "Platform":
                platform = field.value
            if field.name == "URL":
                url = field.value
        if platform and url:
            if uid not in seen_users:
                seen_users[uid] = []
            # Replace existing entry for same platform, otherwise append
            existing = [e for e in seen_users[uid] if e["platform"] != platform]
            existing.append({"user_id": uid, "platform": platform, "url": url})
            seen_users[uid] = existing

    _approved_links_cache      = seen_users
    _approved_links_cache_time = now
    return seen_users

def _invalidate_links_cache():
    global _approved_links_cache, _approved_links_cache_time
    _approved_links_cache      = None
    _approved_links_cache_time = 0

# ── Helper: find approved link for a user ────────────────────────────────────
async def get_approved_link(user_id: int, platform: str = None):
    cache = await _build_links_cache()
    entries = cache.get(str(user_id))
    if not entries:
        return None, None
    # Support list of entries per user (multiple platforms)
    if isinstance(entries, list):
        if platform:
            for e in entries:
                if e["platform"] == platform:
                    return e["platform"], e["url"]
        return entries[0]["platform"], entries[0]["url"]
    # Single entry (legacy)
    if platform and entries["platform"] != platform:
        return None, None
    return entries["platform"], entries["url"]

# ── Helper: get all approved links ───────────────────────────────────────────
async def get_all_approved_links() -> list[dict]:
    cache = await _build_links_cache()
    result = []
    for entries in cache.values():
        if isinstance(entries, list):
            result.extend(entries)
        else:
            result.append(entries)
    return result

# ── YouTube helpers ───────────────────────────────────────────────────────────
def extract_youtube_channel_id_from_url(url: str):
    patterns = [
        (r"youtube\.com/channel/([A-Za-z0-9_\-]+)", "id"),
        (r"youtube\.com/@([A-Za-z0-9_\.\-]+)",       "handle"),
        (r"youtube\.com/c/([A-Za-z0-9_\.\-]+)",      "custom"),
        (r"youtube\.com/user/([A-Za-z0-9_\.\-]+)",   "user"),
    ]
    for pattern, kind in patterns:
        m = re.search(pattern, url)
        if m:
            return kind, m.group(1)
    return None, None

async def fetch_youtube_stats(url: str):
    kind, value = extract_youtube_channel_id_from_url(url)
    if not kind:
        raise ValueError("Couldn't parse that YouTube URL.")

    base = "https://www.googleapis.com/youtube/v3"
    async with aiohttp.ClientSession() as session:

        channel_id = None
        if kind == "id":
            channel_id = value
        elif kind == "handle":
            # Try forHandle first (newer API)
            params = {
                "part": "id,snippet",
                "forHandle": f"@{value}",
                "key": YOUTUBE_API_KEY,
            }
            async with session.get(f"{base}/channels", params=params) as r:
                data = await r.json()
            print(f"[YouTube forHandle] value={value} response={str(data)[:300]}", flush=True)
            items = data.get("items", [])
            if not items:
                # Fallback to search
                params = {
                    "part": "snippet",
                    "q": value,
                    "type": "channel",
                    "maxResults": 1,
                    "key": YOUTUBE_API_KEY,
                }
                async with session.get(f"{base}/search", params=params) as r:
                    data = await r.json()
                print(f"[YouTube search fallback] response={str(data)[:300]}", flush=True)
                items = data.get("items", [])
                if not items:
                    raise ValueError("YouTube channel not found.")
                channel_id = items[0]["snippet"]["channelId"]
            else:
                channel_id = items[0]["id"]
        else:
            params = {
                "part": "id,snippet",
                "forUsername": value,
                "key": YOUTUBE_API_KEY,
            }
            async with session.get(f"{base}/channels", params=params) as r:
                data = await r.json()
            items = data.get("items", [])
            if not items:
                params = {"part": "snippet", "q": value, "type": "channel",
                          "maxResults": 1, "key": YOUTUBE_API_KEY}
                async with session.get(f"{base}/search", params=params) as r:
                    data = await r.json()
                items = data.get("items", [])
                if not items:
                    raise ValueError("YouTube channel not found.")
                channel_id = items[0]["snippet"]["channelId"]
            else:
                channel_id = items[0]["id"]

        params = {
            "part": "snippet,statistics",
            "id": channel_id,
            "key": YOUTUBE_API_KEY,
        }
        async with session.get(f"{base}/channels", params=params) as r:
            data = await r.json()

        items = data.get("items", [])
        if not items:
            raise ValueError("Could not fetch YouTube channel stats.")

        ch      = items[0]
        stats   = ch.get("statistics", {})
        snippet = ch.get("snippet", {})

        # Fetch #dreamyvr videos via search
        dreamyvr_views = 0
        dreamyvr_count = 0
        next_page_token = None
        async with aiohttp.ClientSession() as search_session:
            while True:
                search_params = {
                    "part":       "id",
                    "channelId":  channel_id,
                    "q":          "#dreamyvr",
                    "type":       "video",
                    "maxResults": 50,
                    "key":        YOUTUBE_API_KEY,
                }
                if next_page_token:
                    search_params["pageToken"] = next_page_token
                async with search_session.get(f"{base}/search", params=search_params) as sr:
                    sdata = await sr.json()
                video_ids = [i["id"]["videoId"] for i in sdata.get("items", []) if "videoId" in i.get("id", {})]
                if video_ids:
                    stats_params = {
                        "part": "statistics",
                        "id":   ",".join(video_ids),
                        "key":  YOUTUBE_API_KEY,
                    }
                    async with search_session.get(f"{base}/videos", params=stats_params) as vr:
                        vdata = await vr.json()
                    for v in vdata.get("items", []):
                        dreamyvr_count += 1
                        dreamyvr_views += int(v.get("statistics", {}).get("viewCount", 0))
                next_page_token = sdata.get("nextPageToken")
                if not next_page_token:
                    break

        return {
            "channel_name":   snippet.get("title", "Unknown"),
            "subscribers":    int(stats.get("subscriberCount", 0)),
            "dreamyvr_count": dreamyvr_count,
            "dreamyvr_views": dreamyvr_views,
            "channel_url":    f"https://www.youtube.com/channel/{channel_id}",
        }

# ── TikTok helpers (TikAPI) ──────────────────────────────────────────────────
def extract_tiktok_username(url: str):
    m = re.search(r"tiktok\.com/@([A-Za-z0-9_\.]+)", url)
    return m.group(1) if m else None

async def _tikapi_get(session: aiohttp.ClientSession, endpoint: str, params: dict = {}) -> dict:
    """GET a TikAPI endpoint. Auth via X-API-KEY header."""
    async with session.get(
        f"https://api.tikapi.io/{endpoint}",
        headers={
            "X-API-KEY": TIKAPI_KEY,
            "Accept":    "application/json",
        },
        params=params,
    ) as r:
        raw = await r.text()
        print(f"[TikAPI {endpoint}] status={r.status} raw={raw[:300]}", flush=True)
        if r.status != 200:
            raise ValueError(f"TikAPI returned status {r.status}: {raw[:200]}")
        try:
            return json.loads(raw)
        except json.JSONDecodeError as e:
            raise ValueError(f"TikAPI returned non-JSON: {raw[:200]}") from e

async def fetch_tiktok_posts_data(username: str) -> tuple[int, int, int]:
    """
    Fetches user info + all posts via TikAPI.
    Returns (dreamyvr_views, dreamyvr_count, follower_count).
    """
    total_views    = 0
    video_count    = 0
    follower_count = 0
    cursor         = None
    has_more       = True

    async with aiohttp.ClientSession() as session:
        # Step 1: get user info (followers + secUid)
        sec_uid = None
        try:
            user_data      = await _tikapi_get(session, "public/check", {"username": username})
            user_info      = user_data.get("userInfo", {})
            stats          = user_info.get("stats", {})
            follower_count = int(stats.get("followerCount", 0))
            sec_uid        = user_info.get("user", {}).get("secUid") or user_data.get("secUid")
        except ValueError as e:
            print(f"[TikAPI user error] {e}", flush=True)

        if not sec_uid:
            print(f"[TikAPI] Could not get secUid for {username}", flush=True)
            return 0, 0, follower_count

        # Step 2: paginate posts
        while has_more:
            params = {"secUid": sec_uid, "count": 30}
            if cursor:
                params["cursor"] = cursor
            try:
                data = await _tikapi_get(session, "public/posts", params)
            except ValueError as e:
                print(f"[TikAPI posts error] {e}", flush=True)
                break

            videos   = data.get("itemList", [])
            has_more = bool(data.get("hasMore", False))
            cursor   = data.get("cursor", None)

            if not videos:
                break

            for video in videos:
                desc       = video.get("desc", "").lower()
                challenges = [c.get("title", "").lower() for c in video.get("challenges", [])]
                text_extra = [t.get("hashtagName", "").lower() for t in video.get("textExtra", [])]
                has_tag    = "dreamyvr" in desc or "dreamyvr" in challenges or "dreamyvr" in text_extra
                if has_tag:
                    play_count = (
                        video.get("stats", {}).get("playCount")
                        or video.get("statsV2", {}).get("playCount")
                        or 0
                    )
                    total_views += int(play_count)
                    video_count += 1

    return total_views, video_count, follower_count


async def fetch_tiktok_dreamyvr_views(username: str) -> tuple[int, int]:
    views, count, _ = await fetch_tiktok_posts_data(username)
    return views, count

async def fetch_tiktok_stats(url: str):
    username = extract_tiktok_username(url)
    if not username:
        raise ValueError("Couldn't parse that TikTok URL.")

    async with aiohttp.ClientSession() as session:
        try:
            user_data = await _tikapi_get(session, "public/check", {"username": username})
            nickname  = user_data.get("userInfo", {}).get("user", {}).get("nickname", username)
        except Exception:
            nickname = username

    dreamyvr_views, dreamyvr_count, followers = await fetch_tiktok_posts_data(username)

    return {
        "channel_name":   nickname,
        "username":       username,
        "channel_url":    f"https://www.tiktok.com/@{username}",
        "followers":      followers,
        "dreamyvr_views": dreamyvr_views,
        "dreamyvr_count": dreamyvr_count,
    }

# ── /messageleaderboard ───────────────────────────────────────────────────────
@tree.command(name="messageleaderboard", description="Show the weekly message leaderboard")
async def messageleaderboard(interaction: discord.Interaction):
    await interaction.response.defer()

    counts = await tally_counts()
    if not counts:
        await interaction.followup.send("No messages tracked yet!", ephemeral=True)
        return

    sorted_users = sorted(counts.items(), key=lambda x: x[1], reverse=True)[:10]
    lines = []
    for i, (uid, count) in enumerate(sorted_users):
        member = interaction.guild.get_member(int(uid))
        name   = member.mention if member else f"<@{uid}>"
        lines.append(f"**{i + 1}.** {name} — `{count}` msgs")

    embed = discord.Embed(
        title="Weekly Message Leaderboard",
        description="The top 10 most active members this week:\n\n" + "\n".join(lines),
        color=0x808080,
    )
    embed.set_footer(text="Resets every Sunday at 23:00 UTC")
    await interaction.followup.send(embed=embed)

# ── /link ─────────────────────────────────────────────────────────────────────
@tree.command(name="link", description="Submit your YouTube or TikTok channel link for verification")
@app_commands.describe(platform="Your platform", url="Your YouTube or TikTok channel URL")
async def link(interaction: discord.Interaction, platform: Literal["YouTube", "TikTok"], url: str):
    if interaction.channel_id != LINK_CMD_CHANNEL:
        await interaction.response.send_message(
            f"This command can only be used in <#{LINK_CMD_CHANNEL}>.", ephemeral=True)
        return

    platform_lower = platform.lower()
    if platform_lower == "youtube" and "youtube.com" not in url and "youtu.be" not in url:
        await interaction.response.send_message("That doesn't look like a valid YouTube URL.", ephemeral=True)
        return
    if platform_lower == "tiktok" and "tiktok.com" not in url:
        await interaction.response.send_message("That doesn't look like a valid TikTok URL.", ephemeral=True)
        return

    review_channel = bot.get_channel(LINK_LOG_CHANNEL)
    if not review_channel:
        await interaction.response.send_message("Could not reach the review channel.", ephemeral=True)
        return

    embed = discord.Embed(title="🔗 New Link Request", color=0xFFA500)
    embed.add_field(name="User", value=interaction.user.mention, inline=True)
    embed.add_field(name="Platform", value=platform, inline=True)
    embed.add_field(name="URL", value=url, inline=False)
    embed.set_footer(text=f"User ID: {interaction.user.id}")

    view = LinkReviewView(submitter=interaction.user, platform=platform, url=url, log_channel_id=LINK_LOG_CHANNEL)
    await review_channel.send(embed=embed, view=view)
    await interaction.response.send_message(
        "Your link has been submitted for review. You'll be notified once it's accepted or denied.",
        ephemeral=True)


class LinkReviewView(discord.ui.View):
    def __init__(self, submitter, platform, url, log_channel_id):
        super().__init__(timeout=None)
        self.submitter      = submitter
        self.platform       = platform
        self.url            = url
        self.log_channel_id = log_channel_id

    @discord.ui.button(label="Accept", style=discord.ButtonStyle.green)
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        log_channel = bot.get_channel(self.log_channel_id)
        log_embed = discord.Embed(title="✅ Approved Link", color=0x808080)
        log_embed.add_field(name="User", value=self.submitter.mention, inline=True)
        log_embed.add_field(name="Platform", value=self.platform, inline=True)
        log_embed.add_field(name="URL", value=self.url, inline=False)
        log_embed.set_footer(text=f"Approved by {interaction.user} • User ID: {self.submitter.id}")
        await log_channel.send(embed=log_embed)
        _invalidate_links_cache()

        done_embed = discord.Embed(title="🔗 Link Request — Accepted", color=0x2ECC71)
        done_embed.add_field(name="User", value=self.submitter.mention, inline=True)
        done_embed.add_field(name="Platform", value=self.platform, inline=True)
        done_embed.add_field(name="URL", value=self.url, inline=False)
        done_embed.set_footer(text=f"Accepted by {interaction.user}")
        await interaction.response.edit_message(embed=done_embed, view=None)

        try:
            await self.submitter.send(f"Your {self.platform} link has been **accepted**!\n{self.url}")
        except discord.Forbidden:
            pass

    @discord.ui.button(label="Deny", style=discord.ButtonStyle.red)
    async def deny(self, interaction: discord.Interaction, button: discord.ui.Button):
        done_embed = discord.Embed(title="🔗 Link Request — Denied", color=0xE74C3C)
        done_embed.add_field(name="User", value=self.submitter.mention, inline=True)
        done_embed.add_field(name="Platform", value=self.platform, inline=True)
        done_embed.add_field(name="URL", value=self.url, inline=False)
        done_embed.set_footer(text=f"Denied by {interaction.user}")
        await interaction.response.edit_message(embed=done_embed, view=None)

        try:
            await self.submitter.send(f"Your {self.platform} link submission was **denied**.\n{self.url}")
        except discord.Forbidden:
            pass

# ── /ccstats ──────────────────────────────────────────────────────────────────
@tree.command(name="ccstats", description="View your linked YouTube or TikTok channel stats")
@app_commands.describe(platform="Which platform to show stats for")
async def ccstats(interaction: discord.Interaction, platform: Literal["YouTube", "TikTok"]):
    await interaction.response.send_message("Fetching your stats, please wait...")

    saved_platform, url = await get_approved_link(interaction.user.id, platform)
    if not saved_platform:
        await interaction.edit_original_response(content=f"You don't have an approved **{platform}** link yet. Use `/link` to submit one.")
        return

    try:
        if platform == "YouTube":
            stats = await fetch_youtube_stats(url)
            embed = discord.Embed(title=stats["channel_name"], url=stats["channel_url"], color=0xFF0000)
            embed.add_field(name="Platform",          value="YouTube",                          inline=True)
            embed.add_field(name="Subscribers",       value=f"`{stats['subscribers']:,}`",      inline=True)
            embed.add_field(name="#dreamyvr Videos",  value=f"`{stats['dreamyvr_count']:,}`",   inline=True)
            embed.add_field(name="#dreamyvr Views",   value=f"`{stats['dreamyvr_views']:,}`",   inline=True)
            embed.set_footer(text=f"Requested by {interaction.user}")
            await interaction.edit_original_response(content=None, embed=embed)

        elif platform == "TikTok":
            stats = await fetch_tiktok_stats(url)
            embed = discord.Embed(title=stats["channel_name"], url=stats["channel_url"], color=0x010101)
            embed.add_field(name="Platform",         value="TikTok",                         inline=True)
            embed.add_field(name="Followers",        value=f"`{stats['followers']:,}`",      inline=True)
            embed.add_field(name="#dreamyvr Videos", value=f"`{stats['dreamyvr_count']:,}`", inline=True)
            embed.add_field(name="#dreamyvr Views",  value=f"`{stats['dreamyvr_views']:,}`", inline=True)
            embed.set_footer(text=f"Requested by {interaction.user}")
            await interaction.edit_original_response(content=None, embed=embed)

    except ValueError as e:
        await interaction.edit_original_response(content=f"Error fetching stats: {e}")
    except Exception as e:
        await interaction.edit_original_response(content=f"Something went wrong: {e}")

# ── /message ─────────────────────────────────────────────────────────────────
@tree.command(name="message", description="Send a message as the bot")
@app_commands.describe(content="The message to send")
@app_commands.checks.has_permissions(administrator=True)
async def message(interaction: discord.Interaction, content: str):
    await interaction.channel.send(content)
    await interaction.response.send_message("Message sent!", ephemeral=True)

# ── /adminlink ───────────────────────────────────────────────────────────────
@tree.command(name="adminlink", description="Manually approve a link for a user")
@app_commands.describe(user="The Discord user", platform="Platform", handle="YouTube or TikTok URL/handle")
@app_commands.checks.has_permissions(administrator=True)
async def adminlink(interaction: discord.Interaction, user: discord.Member, platform: Literal["YouTube", "TikTok"], handle: str):
    # Normalise handle into a full URL
    if platform == "YouTube":
        if "youtube.com" not in handle and "youtu.be" not in handle:
            handle = f"https://www.youtube.com/@{handle.lstrip('@')}"
    elif platform == "TikTok":
        if "tiktok.com" not in handle:
            handle = f"https://www.tiktok.com/@{handle.lstrip('@')}"

    log_channel = bot.get_channel(LINK_LOG_CHANNEL)
    if not log_channel:
        await interaction.response.send_message("Could not reach the log channel.", ephemeral=True)
        return

    log_embed = discord.Embed(title="✅ Approved Link", color=0x808080)
    log_embed.add_field(name="User",     value=user.mention,  inline=True)
    log_embed.add_field(name="Platform", value=platform,       inline=True)
    log_embed.add_field(name="URL",      value=handle,         inline=False)
    log_embed.set_footer(text=f"Approved by {interaction.user} • User ID: {user.id}")
    await log_channel.send(embed=log_embed)
    _invalidate_links_cache()

    await interaction.response.send_message(
        f"✅ Manually approved **{platform}** link for {user.mention}: {handle}", ephemeral=True)

# ── /adminccstats ─────────────────────────────────────────────────────────────
@tree.command(name="adminccstats", description="View stats for any linked user")
@app_commands.describe(user="The Discord user to look up", platform="Which platform to show stats for")
@app_commands.checks.has_permissions(administrator=True)
async def adminccstats(interaction: discord.Interaction, user: discord.Member, platform: Literal["YouTube", "TikTok"]):
    await interaction.response.send_message(f"Fetching {platform} stats for {user.mention}, please wait...")

    saved_platform, url = await get_approved_link(user.id, platform)
    if not saved_platform:
        await interaction.edit_original_response(content=f"{user.mention} doesn't have an approved **{platform}** link.")
        return

    try:
        if platform == "YouTube":
            stats = await fetch_youtube_stats(url)
            embed = discord.Embed(title=stats["channel_name"], url=stats["channel_url"], color=0xFF0000)
            embed.add_field(name="Platform",         value="YouTube",                        inline=True)
            embed.add_field(name="Subscribers",      value=f"`{stats['subscribers']:,}`",    inline=True)
            embed.add_field(name="#dreamyvr Videos", value=f"`{stats['dreamyvr_count']:,}`", inline=True)
            embed.add_field(name="#dreamyvr Views",  value=f"`{stats['dreamyvr_views']:,}`", inline=True)
            embed.set_footer(text=f"Requested by {interaction.user} • For {user}")
            await interaction.edit_original_response(content=None, embed=embed)

        elif platform == "TikTok":
            stats = await fetch_tiktok_stats(url)
            embed = discord.Embed(title=stats["channel_name"], url=stats["channel_url"], color=0x010101)
            embed.add_field(name="Platform",         value="TikTok",                         inline=True)
            embed.add_field(name="Followers",        value=f"`{stats['followers']:,}`",      inline=True)
            embed.add_field(name="#dreamyvr Videos", value=f"`{stats['dreamyvr_count']:,}`", inline=True)
            embed.add_field(name="#dreamyvr Views",  value=f"`{stats['dreamyvr_views']:,}`", inline=True)
            embed.set_footer(text=f"Requested by {interaction.user} • For {user}")
            await interaction.edit_original_response(content=None, embed=embed)

    except ValueError as e:
        await interaction.edit_original_response(content=f"Error fetching stats: {e}")
    except Exception as e:
        await interaction.edit_original_response(content=f"Something went wrong: {e}")

# ── /contentfullstats ─────────────────────────────────────────────────────────
@tree.command(name="contentfullstats", description="Show stats for every linked creator")
async def contentfullstats(interaction: discord.Interaction):
    await interaction.response.send_message("⏳ Fetching stats for all linked creators, this may take a moment...")

    all_links = await get_all_approved_links()
    if not all_links:
        await interaction.edit_original_response(content="No approved creator links found.")
        return

    lines  = []
    number = 1

    for entry in all_links:
        uid      = entry["user_id"]
        platform = entry["platform"]
        url      = entry["url"]
        member   = interaction.guild.get_member(int(uid))
        mention  = member.mention if member else f"<@{uid}>"

        try:
            if platform == "TikTok":
                stats = await fetch_tiktok_stats(url)
                lines.append(
                    f"**{number}.** {mention}\n"
                    f"> URL: {url}\n"
                    f"> Followers: {stats['followers']:,}\n"
                    f"> #dreamyvr videos: {stats['dreamyvr_count']:,}\n"
                    f"> #dreamyvr total views: {stats['dreamyvr_views']:,}"
                )
            elif platform == "YouTube":
                stats = await fetch_youtube_stats(url)
                lines.append(
                    f"**{number}.** {mention}\n"
                    f"> URL: {url}\n"
                    f"> Subscribers: {stats['subscribers']:,}\n"
                    f"> #dreamyvr videos: {stats['dreamyvr_count']:,}\n"
                    f"> #dreamyvr total views: {stats['dreamyvr_views']:,}"
                )
            else:
                lines.append(f"**{number}.** {mention}\n> URL: {url}\n> Platform: {platform} (unsupported)")
        except Exception as e:
            lines.append(f"**{number}.** {mention}\n> URL: {url}\n> ⚠️ Error: {e}")

        number += 1

    # Chunk into <=1900 char messages
    chunks  = []
    current = ""
    for line in lines:
        block = line + "\n\n"
        if len(current) + len(block) > 1900:
            chunks.append(current.strip())
            current = block
        else:
            current += block
    if current.strip():
        chunks.append(current.strip())

    if not chunks:
        await interaction.edit_original_response(content="No data to display.")
        return

    await interaction.edit_original_response(content=chunks[0])
    for chunk in chunks[1:]:
        await interaction.followup.send(chunk)

# ── Weekly reset ──────────────────────────────────────────────────────────────
@tasks.loop(time=RESET_TIME)
async def weekly_reset():
    now = datetime.now(TIMEZONE)
    if now.weekday() != RESET_WEEKDAY:
        return
    db_channel = bot.get_channel(DB_CHANNEL_ID)
    if not db_channel:
        print("Could not find DB channel — reset aborted.")
        return
    deleted = await db_channel.purge(limit=None)
    print(f"[{now}] Weekly reset — deleted {len(deleted)} log entries.")


# In-memory streak state — loaded from DB channel on startup
# { user_id: { streak, messages_today, last_streak_time, window_open } }
streak_data: dict = {}

# ── Streak DB helpers ─────────────────────────────────────────────────────────
def _streak_encode(uid: str, data: dict) -> str:
    import json
    return f"STREAK:{uid}:{json.dumps(data)}"

async def _save_streak(uid: str):
    import json
    db = bot.get_channel(STREAK_DB_CHANNEL)
    if not db:
        return
    payload = json.dumps(streak_data[uid])
    await db.send(f"STREAK:{uid}:{payload}")

async def _load_streaks():
    import json
    db = bot.get_channel(STREAK_DB_CHANNEL)
    if not db:
        return
    latest = {}  # uid -> latest record
    async for msg in db.history(limit=None, oldest_first=True):
        if not msg.author.bot:
            continue
        content = msg.content
        if not content.startswith("STREAK:"):
            continue
        parts = content.split(":", 2)
        if len(parts) != 3:
            continue
        uid = parts[1]
        try:
            latest[uid] = json.loads(parts[2])
        except Exception:
            continue
    streak_data.update(latest)
    print(f"[Streaks] Loaded {len(streak_data)} streak records.", flush=True)

def _get_streak(uid: str) -> dict:
    return streak_data.get(uid, {
        "streak":          0,
        "messages_today":  0,
        "last_streak_time": None,  # ISO timestamp of when last streak was earned
        "pending":         False,  # True when window is open and waiting for 3 msgs
    })

async def _update_streak_roles(member: discord.Member, streak: int):
    """Give the highest earned role, remove all lower ones."""
    guild = member.guild
    earned_role_id = None
    for threshold in sorted(STREAK_ROLES.keys()):
        if streak >= threshold:
            earned_role_id = STREAK_ROLES[threshold]

    for threshold, role_id in STREAK_ROLES.items():
        role = guild.get_role(role_id)
        if not role:
            continue
        if role_id == earned_role_id:
            if role not in member.roles:
                await member.add_roles(role)
        else:
            if role in member.roles:
                await member.remove_roles(role)

async def _remove_all_streak_roles(member: discord.Member):
    guild = member.guild
    for role_id in STREAK_ROLES.values():
        role = guild.get_role(role_id)
        if role and role in member.roles:
            await member.remove_roles(role)

# ── Streak checker loop ───────────────────────────────────────────────────────
@tasks.loop(minutes=5)
async def streak_checker():
    from datetime import datetime, timezone, timedelta
    import json
    now = datetime.now(timezone.utc)
    to_break = []

    for uid, data in streak_data.items():
        if not data.get("last_streak_time"):
            continue
        last = datetime.fromisoformat(data["last_streak_time"])
        elapsed = (now - last).total_seconds() / 3600

        # Window opens after 24h and closes after 28h
        window_start = STREAK_WINDOW_HOURS
        window_end   = STREAK_WINDOW_HOURS + STREAK_GRACE_HOURS

        if elapsed >= window_end and data.get("streak", 0) > 0:
            # Missed the window — break streak
            to_break.append(uid)

    for uid in to_break:
        data = streak_data[uid]
        old_streak = data["streak"]
        data["streak"]         = 0
        data["messages_today"] = 0
        data["pending"]        = False
        await _save_streak(uid)

        # Announce
        announce = bot.get_channel(STREAK_ANNOUNCE_CHANNEL)
        if announce:
            await announce.send(f"💔 <@{uid}>, you have lost your streak of `{old_streak}`!")

        # Remove roles
        for guild in bot.guilds:
            member = guild.get_member(int(uid))
            if member:
                await _remove_all_streak_roles(member)

# ── Streak message handler ────────────────────────────────────────────────────
async def handle_streak(message: discord.Message):
    from datetime import datetime, timezone
    uid  = str(message.author.id)
    now  = datetime.now(timezone.utc)
    data = _get_streak(uid)

    last_time     = datetime.fromisoformat(data["last_streak_time"]) if data.get("last_streak_time") else None
    elapsed_hours = (now - last_time).total_seconds() / 3600 if last_time else None

    # Past grace window — reset message count so they can start fresh
    if elapsed_hours is not None and elapsed_hours > STREAK_WINDOW_HOURS + STREAK_GRACE_HOURS:
        data["messages_today"] = 0

    # Count this message
    data["messages_today"] = data.get("messages_today", 0) + 1
    streak_data[uid] = data
    print(f"[Streak] {message.author} msgs={data['messages_today']} streak={data.get('streak',0)} elapsed={elapsed_hours}", flush=True)

    if data["messages_today"] >= MESSAGES_REQUIRED:
        in_window    = last_time is not None and STREAK_WINDOW_HOURS <= elapsed_hours <= STREAK_WINDOW_HOURS + STREAK_GRACE_HOURS
        no_streak_yet = last_time is None or data.get("streak", 0) == 0

        if in_window or no_streak_yet:
            data["streak"]           = data.get("streak", 0) + 1
            data["messages_today"]   = 0
            data["last_streak_time"] = now.isoformat()
            streak_data[uid]         = data
            await _save_streak(uid)

            announce = bot.get_channel(STREAK_ANNOUNCE_CHANNEL)
            if announce:
                await announce.send(
                    f"<:Sneeze:1495243609035899023> <@{uid}>, you have acquired a chat streak!\n"
                    f"**Streak:** `{data['streak']}`"
                )

            member = message.guild.get_member(message.author.id) if message.guild else None
            if member:
                await _update_streak_roles(member, data["streak"])
        else:
            await _save_streak(uid)
    else:
        await _save_streak(uid)

# ── Member count voice channel ───────────────────────────────────────────────
MEMBER_COUNT_CHANNEL_ID = 1512865382782865529

async def update_member_count(guild: discord.Guild):
    channel = guild.get_channel(MEMBER_COUNT_CHANNEL_ID)
    if channel:
        await channel.edit(name=f"Members: {guild.member_count}")

@bot.event
async def on_member_join(member: discord.Member):
    await update_member_count(member.guild)

@bot.event
async def on_member_remove(member: discord.Member):
    await update_member_count(member.guild)

# ── Ticket config ────────────────────────────────────────────────────────────
TICKET_PANEL_CHANNEL = 1495162997734117386

# ── Ticket panel ─────────────────────────────────────────────────────────────
class TicketSelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(
                label="Dreamy VR Discord Support",
                emoji=discord.PartialEmoji(name="UhOkay", id=1495243635132731702),
                value="discord_support",
                description="Get help with Discord-related issues",
            ),
            discord.SelectOption(
                label="Dreamy VR In-Game Support",
                emoji=discord.PartialEmoji(name="UhOkay", id=1495243635132731702),
                value="ingame_support",
                description="Get help with in-game issues",
            ),
        ]
        super().__init__(
            placeholder="Click the option that best matches your issue...",
            min_values=1,
            max_values=1,
            options=options,
            custom_id="ticket_select",
        )

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_message(
            f"You selected **{self.values[0]}** — ticket creation coming soon!",
            ephemeral=True,
        )

class TicketPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(TicketSelect())

async def post_ticket_panel():
    channel = bot.get_channel(TICKET_PANEL_CHANNEL)
    if not channel:
        print("[Tickets] Could not find ticket panel channel.", flush=True)
        return

    # Delete old bot messages in the channel
    async for msg in channel.history(limit=50):
        if msg.author == bot.user:
            await msg.delete()

    embed = discord.Embed(
        title="🎫  How to Create a Ticket",
        description="Click the option from the dropdown menu that best matches your reason for opening a ticket.",
        color=0x2b2d31,
    )
    embed.set_author(
        name="Dreamy VR Support System",
        icon_url=channel.guild.icon.url if channel.guild.icon else None,
    )
    embed.add_field(
        name="📋  Ticket Rules",
        value=(
            "`1.` Only open tickets for valid issues such as in-game problems or Discord-related reports.\n"
            "`2.` Do not open tickets to ask for staff roles, free items, or currency.\n"
            "`3.` Do not use tickets to report bugs, use the proper bug report channel instead.\n"
            "`4.` Be respectful and provide clear, detailed information about your issue.\n"
            "`5.` Do not spam or open multiple tickets for the same issue."
        ),
        inline=False,
    )
    embed.set_image(url="https://cdn.discordapp.com/attachments/1495388852020445255/1495396291914629362/SUPORTTICKETS.png?ex=6a313d53&is=6a2febd3&hm=ac38999087c38cc8f5687a7c8c1e16aa5d71f8f568e3c123cef33ff2693b012c")

    await channel.send(embed=embed, view=TicketPanelView())
    print("[Tickets] Panel posted.", flush=True)

# ── Startup ───────────────────────────────────────────────────────────────────
@bot.event
async def on_ready():
    bot.add_view(TicketPanelView())
    await tree.sync()
    weekly_reset.start()
    streak_checker.start()
    await _load_streaks()
    await post_ticket_panel()
    print(f"Logged in as {bot.user} ({bot.user.id})")

bot.run(TOKEN)
