import os
import re
import asyncio
import aiohttp
import discord
from discord.ext import commands, tasks

# ── Config ──────────────────────────────────────────────────────────────
YOUTUBE_CHANNEL_HANDLE  = "dreamyvrofficial"
TIKTOK_USERNAME         = "dreamyvrofficial"
DISCORD_CHANNEL_ID      = 1512853017920143560
MEMBER_COUNT_CHANNEL_ID = 1512865382782865529
FORUM_CHANNEL_ID        = 1498288028630913055
REQUIRED_TAG_ID         = 1512877289900081305
SUBMISSIONS_CHANNEL_ID  = 1512877823168090173
ROLE_MENTION            = "<@&1512854249174863882>"
CHECK_INTERVAL          = 300  # 5 minutes

DISCORD_TOKEN   = os.environ["DISCORD_TOKEN"]
YOUTUBE_API_KEY = os.environ["YOUTUBE_API_KEY"]
# ────────────────────────────────────────────────────────────────────────

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)

last_youtube_video_id    = None
last_tiktok_video_id     = None
youtube_channel_id_cache = None
processed_forum_posts    = set()

YOUTUBE_REGEX = re.compile(r'https?://(?:www\.)?(?:youtube\.com/watch\?v=|youtu\.be/)([\w-]+)')
TIKTOK_REGEX  = re.compile(r'https?://(?:www\.)?tiktok\.com/@([\w.]+)/video/(\d+)')


# ── Member count ─────────────────────────────────────────────────────────

async def update_member_count(guild: discord.Guild):
    channel = guild.get_channel(MEMBER_COUNT_CHANNEL_ID)
    if channel:
        await channel.edit(name=f"☁️・Members: {guild.member_count}")

@bot.event
async def on_member_join(member: discord.Member):
    await update_member_count(member.guild)

@bot.event
async def on_member_remove(member: discord.Member):
    await update_member_count(member.guild)


# ── Video description fetchers ───────────────────────────────────────────

async def get_youtube_description(session: aiohttp.ClientSession, video_id: str) -> str:
    url = "https://www.googleapis.com/youtube/v3/videos"
    params = {
        "part": "snippet",
        "id": video_id,
        "key": YOUTUBE_API_KEY,
    }
    try:
        async with session.get(url, params=params) as r:
            data = await r.json()
            items = data.get("items", [])
            if items:
                snippet = items[0]["snippet"]
                tags = " ".join(snippet.get("tags", []))
                return f"{snippet.get('title', '')} {snippet.get('description', '')} {tags}"
    except Exception as e:
        print(f"YouTube description fetch error: {e}")
    return ""


async def get_tiktok_description(session: aiohttp.ClientSession, video_id: str, username: str) -> str:
    url = f"https://www.tiktok.com/@{username}/video/{video_id}"
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
            match = re.search(r'<meta name="description" content="([^"]*)"', html)
            if match:
                return match.group(1)
            return html
    except Exception as e:
        print(f"TikTok description fetch error: {e}")
    return ""


async def video_has_dreamyvr_tag(session: aiohttp.ClientSession, content: str) -> bool:
    for match in YOUTUBE_REGEX.finditer(content):
        video_id = match.group(1)
        description = await get_youtube_description(session, video_id)
        if re.search(r'#dreamyvr', description, re.IGNORECASE):
            return True

    for match in TIKTOK_REGEX.finditer(content):
        username = match.group(1)
        video_id = match.group(2)
        description = await get_tiktok_description(session, video_id, username)
        if re.search(r'#dreamyvr', description, re.IGNORECASE):
            return True

    return False


def has_required_tag(thread: discord.Thread) -> bool:
    if not hasattr(thread, 'applied_tags'):
        return False
    return any(tag.id == REQUIRED_TAG_ID for tag in thread.applied_tags)

def has_video_link(content: str) -> bool:
    return bool(YOUTUBE_REGEX.search(content) or TIKTOK_REGEX.search(content))

def already_reacted(message: discord.Message) -> bool:
    for reaction in message.reactions:
        if str(reaction.emoji) == "👍":
            return reaction.me
    return False


# ── Forum processor ──────────────────────────────────────────────────────

async def process_thread(thread: discord.Thread, session: aiohttp.ClientSession):
    try:
        if not has_required_tag(thread):
            return

        messages = [msg async for msg in thread.history(limit=1, oldest_first=True)]
        if not messages:
            return
        first_message = messages[0]
        content = first_message.content

        if not has_video_link(content):
            return

        if not await video_has_dreamyvr_tag(session, content):
            return

        # React with 👍
        if not already_reacted(first_message):
            await first_message.add_reaction("👍")

        # Post to submissions channel
        if thread.id not in processed_forum_posts:
            submissions_channel = bot.get_channel(SUBMISSIONS_CHANNEL_ID)
            if submissions_channel:
                post_link = f"https://discord.com/channels/{thread.guild.id}/{thread.id}/{first_message.id}"
                await submissions_channel.send(
                    f"📹 New community video submission from {first_message.author.mention}!\n{post_link}"
                )
            processed_forum_posts.add(thread.id)

    except Exception as e:
        print(f"Error processing thread {thread.id}: {e}")


@tasks.loop(seconds=CHECK_INTERVAL)
async def check_forum():
    forum_channel = bot.get_channel(FORUM_CHANNEL_ID)
    if not forum_channel or not isinstance(forum_channel, discord.ForumChannel):
        print("Forum channel not found or not a ForumChannel!")
        return

    print(f"Checking forum... ({len(forum_channel.threads)} active threads)")

    async with aiohttp.ClientSession() as session:
        for thread in forum_channel.threads:
            await process_thread(thread, session)
            await asyncio.sleep(0.5)

        try:
            async for thread in forum_channel.archived_threads(limit=100):
                await process_thread(thread, session)
                await asyncio.sleep(0.5)
        except Exception as e:
            print(f"Error fetching archived threads: {e}")

@check_forum.before_loop
async def before_check_forum():
    await bot.wait_until_ready()


# ── YouTube channel helpers ──────────────────────────────────────────────

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


# ── TikTok latest video ──────────────────────────────────────────────────

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


# ── Social media check loop ──────────────────────────────────────────────

@tasks.loop(seconds=CHECK_INTERVAL)
async def check_socials():
    global last_youtube_video_id, last_tiktok_video_id

    async with aiohttp.ClientSession() as session:
        yt_id, yt_url = await get_latest_youtube_video(session)
        if yt_id and yt_id != last_youtube_video_id:
            if last_youtube_video_id is not None:
                await send_notification(yt_url)
            last_youtube_video_id = yt_id

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

    for guild in bot.guilds:
        await update_member_count(guild)

    check_socials.start()
    check_forum.start()


bot.run(DISCORD_TOKEN)
