import discord
from discord.ext import commands, tasks
from discord import app_commands
import os
import re
import aiohttp
from datetime import datetime, time
import pytz
from typing import Literal

# ── Config ───────────────────────────────────────────────────────────────────
TOKEN              = os.environ["DISCORD_BOT_TOKEN"]
YOUTUBE_API_KEY    = "AIzaSyAe5hyEAwxTCdBbZRQQsGfuQC6xlQWUBg04"
RAPIDAPI_KEY       = "ae92fc6f3bmsh5bacceeaaecb548p164d3ejsn0ef636bc8645"
DB_CHANNEL_ID      = 1515064641246466113
LINK_CMD_CHANNEL   = 1513272619439226980
LINK_LOG_CHANNEL   = 1512899799077093546
RESET_TIME         = time(23, 0)
RESET_WEEKDAY      = 6
TIMEZONE           = pytz.timezone("UTC")

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
    if message.channel.id in (DB_CHANNEL_ID, 1500327292830875898):
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

# ── Helper: find approved link for a user ────────────────────────────────────
async def get_approved_link(user_id: int):
    """
    Scan LINK_LOG_CHANNEL for an approved embed belonging to this user.
    Returns (platform, url) or (None, None).
    """
    log_channel = bot.get_channel(LINK_LOG_CHANNEL)
    uid_str = str(user_id)
    async for msg in log_channel.history(limit=None, oldest_first=False):
        if not msg.embeds:
            continue
        embed = msg.embeds[0]
        # Footer contains "User ID: <id>"
        if embed.footer and embed.footer.text and uid_str in embed.footer.text:
            platform = None
            url      = None
            for field in embed.fields:
                if field.name == "Platform":
                    platform = field.value
                if field.name == "URL":
                    url = field.value
            if platform and url:
                return platform, url
    return None, None

# ── YouTube helpers ───────────────────────────────────────────────────────────
def extract_youtube_channel_id_from_url(url: str):
    """
    Handles:
      youtube.com/channel/UCxxxx
      youtube.com/@handle
      youtube.com/c/name
      youtube.com/user/name
    Returns a (type, value) tuple: ("id", "UCxxx") or ("handle", "@name") etc.
    """
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
    """
    Returns dict with keys: channel_name, subscribers, total_views, video_count, channel_url
    or raises ValueError with a human-readable message.
    """
    kind, value = extract_youtube_channel_id_from_url(url)
    if not kind:
        raise ValueError("Couldn't parse that YouTube URL.")

    base = "https://www.googleapis.com/youtube/v3"
    async with aiohttp.ClientSession() as session:

        # Step 1 — resolve to a channel ID if needed
        channel_id = None
        if kind == "id":
            channel_id = value
        elif kind == "handle":
            # Use search to resolve @handle
            params = {
                "part": "snippet",
                "q": f"@{value}",
                "type": "channel",
                "maxResults": 1,
                "key": YOUTUBE_API_KEY,
            }
            async with session.get(f"{base}/search", params=params) as r:
                data = await r.json()
            items = data.get("items", [])
            if not items:
                raise ValueError("YouTube channel not found.")
            channel_id = items[0]["snippet"]["channelId"]
        else:
            # custom / user — use forUsername or search
            params = {
                "part": "id,snippet",
                "forUsername": value,
                "key": YOUTUBE_API_KEY,
            }
            async with session.get(f"{base}/channels", params=params) as r:
                data = await r.json()
            items = data.get("items", [])
            if not items:
                # fallback to search
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

        # Step 2 — fetch statistics
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

        ch       = items[0]
        stats    = ch.get("statistics", {})
        snippet  = ch.get("snippet", {})
        return {
            "channel_name":  snippet.get("title", "Unknown"),
            "subscribers":   int(stats.get("subscriberCount", 0)),
            "total_views":   int(stats.get("viewCount", 0)),
            "video_count":   int(stats.get("videoCount", 0)),
            "channel_url":   f"https://www.youtube.com/channel/{channel_id}",
        }

# ── TikTok helpers ────────────────────────────────────────────────────────────
def extract_tiktok_username(url: str):
    m = re.search(r"tiktok\.com/@([A-Za-z0-9_\.]+)", url)
    return m.group(1) if m else None

async def fetch_tiktok_dreamyvr_views(username: str) -> tuple[int, int]:
    """
    Paginates through all user posts, filters to videos tagged #dreamyvr,
    and returns (total_dreamyvr_views, dreamyvr_video_count).
    """
    headers = {
        "x-rapidapi-host": "tiktok-api23.p.rapidapi.com",
        "x-rapidapi-key":  RAPIDAPI_KEY,
        "Content-Type":    "application/json",
    }
    total_views  = 0
    video_count  = 0
    cursor       = 0
    has_more     = True

    async with aiohttp.ClientSession() as session:
        while has_more:
            params = {"uniqueId": username, "count": "30", "cursor": str(cursor)}
            async with session.get(
                "https://tiktok-api23.p.rapidapi.com/api/user/posts",
                headers=headers,
                params=params,
            ) as r:
                if r.status != 200:
                    break
                data = await r.json()

            videos   = data.get("data", {}).get("videos", data.get("itemList", []))
            has_more = data.get("data", {}).get("hasMore", data.get("hasMore", False))
            cursor   = data.get("data", {}).get("cursor", data.get("cursor", 0))

            for video in videos:
                desc       = video.get("desc", "").lower()
                challenges = [c.get("title", "").lower() for c in video.get("challenges", [])]
                # also check textExtra which TikTok uses for hashtags
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

            if not videos:
                break

    return total_views, video_count

async def fetch_tiktok_stats(url: str):
    """
    Fetches user info + #dreamyvr video views from tiktok-api23.p.rapidapi.com.
    """
    username = extract_tiktok_username(url)
    if not username:
        raise ValueError("Couldn't parse that TikTok URL.")

    headers = {
        "x-rapidapi-host": "tiktok-api23.p.rapidapi.com",
        "x-rapidapi-key":  RAPIDAPI_KEY,
        "Content-Type":    "application/json",
    }

    async with aiohttp.ClientSession() as session:
        async with session.get(
            "https://tiktok-api23.p.rapidapi.com/api/user/info",
            headers=headers,
            params={"uniqueId": username},
        ) as r:
            if r.status != 200:
                raise ValueError(f"TikTok API returned status {r.status}.")
            data = await r.json()

    try:
        user  = data["userInfo"]["user"]
        stats = data["userInfo"]["stats"]
    except KeyError:
        raise ValueError(f"Unexpected response from TikTok API. Raw: {str(data)[:500]}")

    dreamyvr_views, dreamyvr_count = await fetch_tiktok_dreamyvr_views(username)

    return {
        "channel_name":    user.get("nickname", username),
        "username":        username,
        "channel_url":     f"https://www.tiktok.com/@{username}",
        "followers":       int(stats.get("followerCount", 0)),
        "dreamyvr_views":  dreamyvr_views,
        "dreamyvr_count":  dreamyvr_count,
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
async def ccstats(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=False)

    platform, url = await get_approved_link(interaction.user.id)
    if not platform:
        await interaction.followup.send(
            "You don't have an approved channel link yet. Use `/link` to submit one.",
            ephemeral=True
        )
        return

    try:
        if platform == "YouTube":
            stats = await fetch_youtube_stats(url)

            embed = discord.Embed(
                title=f"{stats['channel_name']}",
                url=stats["channel_url"],
                color=0xFF0000,
            )
            embed.add_field(name="Platform",     value="YouTube",                              inline=True)
            embed.add_field(name="Subscribers",  value=f"`{stats['subscribers']:,}`",          inline=True)
            embed.add_field(name="Total Views",  value=f"`{stats['total_views']:,}`",           inline=True)
            embed.add_field(name="Videos",       value=f"`{stats['video_count']:,}`",           inline=True)
            embed.set_footer(text=f"Requested by {interaction.user}")
            await interaction.followup.send(embed=embed)

        elif platform == "TikTok":
            stats = await fetch_tiktok_stats(url)

            embed = discord.Embed(
                title=stats["channel_name"],
                url=stats["channel_url"],
                color=0x010101,
            )
            embed.add_field(name="Platform",         value="TikTok",                               inline=True)
            embed.add_field(name="Followers",        value=f"`{stats['followers']:,}`",            inline=True)
            embed.add_field(name="#dreamyvr Views",  value=f"`{stats['dreamyvr_views']:,}`",       inline=True)
            embed.add_field(name="#dreamyvr Videos", value=f"`{stats['dreamyvr_count']:,}`",       inline=True)
            embed.set_footer(text=f"Requested by {interaction.user}")
            await interaction.followup.send(embed=embed)

    except ValueError as e:
        await interaction.followup.send(f"Error fetching stats: {e}", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"Something went wrong: {e}", ephemeral=True)

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

# ── Startup ───────────────────────────────────────────────────────────────────
@bot.event
async def on_ready():
    await tree.sync()
    weekly_reset.start()
    print(f"Logged in as {bot.user} ({bot.user.id})")

bot.run(TOKEN)
