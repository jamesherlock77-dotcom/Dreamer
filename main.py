from dotenv import load_dotenv
load_dotenv()
import os
import re
import aiohttp
import discord
from discord.ext import commands, tasks

# ── Config ──────────────────────────────────────────────────────────────
YOUTUBE_CHANNEL_HANDLE = "dreamyvrofficial"
TIKTOK_USERNAME        = "dreamyvrofficial"
DISCORD_CHANNEL_ID     = 1512853017920143560
ROLE_MENTION           = "<@&1512854249174863882>"
CHECK_INTERVAL         = 300  # 5 minutes in seconds

DISCORD_TOKEN   = os.environ["DISCORD_TOKEN"]
YOUTUBE_API_KEY = os.environ["YOUTUBE_API_KEY"]
# ────────────────────────────────────────────────────────────────────────

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

last_youtube_video_id    = None
last_tiktok_video_id     = None
youtube_channel_id_cache = None


# ── YouTube helpers ──────────────────────────────────────────────────────

async def get_youtube_channel_id(session: aiohttp.ClientSession) -> str | None:
    global youtube_channel_id_cache
    if youtube_channel_id_cache:
        return youtube_channel_id_cache

    url = "https://www.googleapis.com/youtube/v3/channels"
    params = {"part": "id", "forHandle": YOUTUBE_CHANNEL_HANDLE, "key": YOUTUBE_API_KEY}
    async with session.get(url, params=params) as r:
        data = await r.json()
        items = data.get("items", [])
        if items:
            youtube_channel_id_cache = items[0]["id"]
            return youtube_channel_id_cache
    return None


async def get_latest_youtube_video(session: aiohttp.ClientSession):
    channel_id = await get_youtube_channel_id(session)
    if not channel_id:
        return None, None

    url = "https://www.googleapis.com/youtube/v3/search"
    params = {
        "part": "snippet",
        "channelId": channel_id,
        "order": "date",
        "maxResults": 1,
        "type": "video",
        "key": YOUTUBE_API_KEY,
    }
    async with session.get(url, params=params) as r:
        data = await r.json()
        items = data.get("items", [])
        if items:
            vid_id = items[0]["id"]["videoId"]
            return vid_id, f"https://www.youtube.com/watch?v={vid_id}"
    return None, None


# ── TikTok helpers ───────────────────────────────────────────────────────

async def get_latest_tiktok_video(session: aiohttp.ClientSession):
    url = f"https://www.tiktok.com/@{TIKTOK_USERNAME}"
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0 Safari/537.36"
        )
    }
    try:
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as r:
            html = await r.text()
            matches = re.findall(r'/@' + TIKTOK_USERNAME + r'/video/(\d+)', html)
            if matches:
                vid_id = matches[0]
                return vid_id, f"https://www.tiktok.com/@{TIKTOK_USERNAME}/video/{vid_id}"
    except Exception as e:
        print(f"TikTok scrape error: {e}")
    return None, None


# ── Notification helper ──────────────────────────────────────────────────

async def send_notification(video_url: str):
    channel = bot.get_channel(DISCORD_CHANNEL_ID)
    if channel:
        await channel.send(
            f"Hey everyone, we just posted a video! Go check it out!\n"
            f"{ROLE_MENTION}\n"
            f"{video_url}"
        )


# ── Background task ──────────────────────────────────────────────────────

@tasks.loop(seconds=CHECK_INTERVAL)
async def check_socials():
    global last_youtube_video_id, last_tiktok_video_id

    async with aiohttp.ClientSession() as session:
        # — YouTube —
        yt_id, yt_url = await get_latest_youtube_video(session)
        if yt_id and yt_id != last_youtube_video_id:
            if last_youtube_video_id is not None:
                await send_notification(yt_url)
            last_youtube_video_id = yt_id

        # — TikTok —
        tt_id, tt_url = await get_latest_tiktok_video(session)
        if tt_id and tt_id != last_tiktok_video_id:
            if last_tiktok_video_id is not None:
                await send_notification(tt_url)
            last_tiktok_video_id = tt_id


@check_socials.before_loop
async def before_check():
    await bot.wait_until_ready()


# ── Slash command: /testsocialmedia ─────────────────────────────────────

@bot.tree.command(name="testsocialmedia", description="Test the social media notification format")
async def testsocialmedia(interaction: discord.Interaction):
    await interaction.response.defer()

    async with aiohttp.ClientSession() as session:
        yt_id, yt_url = await get_latest_youtube_video(session)
        tt_id, tt_url = await get_latest_tiktok_video(session)

    lines = []

    if yt_url:
        lines.append(
            f"**YouTube** ✅\n"
            f"Hey everyone, we just posted a video! Go check it out!\n"
            f"{ROLE_MENTION}\n"
            f"{yt_url}"
        )
    else:
        lines.append("**YouTube** ❌ Could not fetch latest video.")

    if tt_url:
        lines.append(
            f"**TikTok** ✅\n"
            f"Hey everyone, we just posted a video! Go check it out!\n"
            f"{ROLE_MENTION}\n"
            f"{tt_url}"
        )
    else:
        lines.append("**TikTok** ❌ Could not fetch latest video.")

    await interaction.followup.send("\n\n".join(lines))


# ── Bot startup ──────────────────────────────────────────────────────────

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} slash command(s)")
    except Exception as e:
        print(f"Failed to sync commands: {e}")
    check_socials.start()


bot.run(DISCORD_TOKEN)
