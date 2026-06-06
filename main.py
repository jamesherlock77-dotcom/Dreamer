import os
import re
import asyncio
import aiohttp
import discord
from discord.ext import commands, tasks
from datetime import datetime, timezone, timedelta
 
# ── Config ──────────────────────────────────────────────────────────────
YOUTUBE_CHANNEL_HANDLE  = "dreamyvrofficial"
TIKTOK_USERNAME         = "dreamyvrofficial"
DISCORD_CHANNEL_ID      = 1512853017920143560
MEMBER_COUNT_CHANNEL_ID = 1512865382782865529
FORUM_CHANNEL_ID        = 1498288028630913055
REQUIRED_TAG_ID         = 1512877289900081305
SUBMISSIONS_CHANNEL_ID  = 1512877823168090173
CC_DATABASE_CHANNEL_ID  = 1512899799077093546
CC_ROLE_ID              = 1495165348654219344
ROLE_MENTION            = "<@&1512854249174863882>"
CHECK_INTERVAL          = 300  # 5 minutes
 
DISCORD_TOKEN   = os.environ["DISCORD_TOKEN"]
YOUTUBE_API_KEY = os.environ["YOUTUBE_API_KEY"]
RAPIDAPI_KEY    = os.environ["RAPIDAPI_KEY"]
 
# ── Tier config (role_id, view threshold, label) ─────────────────────────
# Tiers are ordered lowest to highest. View threshold of 0 = video-count only tier.
TIERS = [
    {"role_id": 1512870343310835974, "min_views": 0,      "min_videos": 5,  "label": "5+ Videos"},
    {"role_id": 1512871854308589648, "min_views": 5000,   "min_videos": 0,  "label": "5k Views"},
    {"role_id": 1512871100361478375, "min_views": 10000,  "min_videos": 0,  "label": "10k Views"},
    {"role_id": 1512872854926917874, "min_views": 20000,  "min_videos": 0,  "label": "20k Views"},
    {"role_id": 1512873532374122516, "min_views": 30000,  "min_videos": 0,  "label": "30k Views"},
    {"role_id": 1512874309641699399, "min_views": 50000,  "min_videos": 0,  "label": "50k Views"},
    {"role_id": 1512874841240375436, "min_views": 100000, "min_videos": 0,  "label": "100k Views"},
]
# ────────────────────────────────────────────────────────────────────────
 
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)
 
last_youtube_video_id    = None
last_tiktok_video_id     = None
youtube_channel_id_cache = None
processed_forum_posts    = set()
 
# In-memory CC database:
# {user_id: {"youtube": {"handle": "...", "confirmed": bool}, "tiktok": {"handle": "...", "confirmed": bool}}}
cc_database = {}
 
# Tracks the highest tier index (into TIERS list) each user has already been notified for.
# {user_id: int}  — -1 means no tier yet
user_tier_cache = {}
 
YOUTUBE_REGEX = re.compile(r'https?://(?:www\.)?(?:youtube\.com/watch\?v=|youtu\.be/)([\w-]+)')
TIKTOK_REGEX  = re.compile(r'https?://(?:www\.)?tiktok\.com/@([\w.]+)/video/(\d+)')
 
# ── Database channel parser ───────────────────────────────────────────────
 
async def load_cc_database():
    """On startup, read the database channel and rebuild cc_database."""
    global cc_database
    channel = bot.get_channel(CC_DATABASE_CHANNEL_ID)
    if not channel:
        print("CC database channel not found!")
        return
 
    print("Loading CC database from channel history...")
    cc_database = {}
 
    async for message in channel.history(limit=1000, oldest_first=True):
        if not message.author.bot:
            continue
 
        content = message.content
 
        # Format: ✅ CONFIRMED | USER_ID:<id> | PLATFORM:<platform> | HANDLE:<handle>
        confirmed_match = re.search(
            r'✅ CONFIRMED \| USER_ID:(\d+) \| PLATFORM:(\w+) \| HANDLE:(.+)',
            content
        )
        if confirmed_match:
            user_id  = int(confirmed_match.group(1))
            platform = confirmed_match.group(2)
            handle   = confirmed_match.group(3).strip()
            if user_id not in cc_database:
                cc_database[user_id] = {}
            cc_database[user_id][platform] = {"handle": handle, "confirmed": True}
            continue
 
        # Format: ❌ DECLINED | USER_ID:<id> | PLATFORM:<platform>
        declined_match = re.search(r'❌ DECLINED \| USER_ID:(\d+) \| PLATFORM:(\w+)', content)
        if declined_match:
            user_id  = int(declined_match.group(1))
            platform = declined_match.group(2)
            if user_id in cc_database:
                cc_database[user_id].pop(platform, None)
            continue
 
        # Format: 🕐 PENDING | USER_ID:<id> | PLATFORM:<platform> | HANDLE:<handle>
        pending_match = re.search(
            r'🕐 PENDING \| USER_ID:(\d+) \| PLATFORM:(\w+) \| HANDLE:(.+)',
            content
        )
        if pending_match:
            user_id  = int(pending_match.group(1))
            platform = pending_match.group(2)
            handle   = pending_match.group(3).strip()
            if user_id not in cc_database:
                cc_database[user_id] = {}
            if platform not in cc_database[user_id]:
                cc_database[user_id][platform] = {"handle": handle, "confirmed": False}
 
    print(f"Loaded {len(cc_database)} CC entries.")
 
 
# ── CC Confirm/Decline buttons ────────────────────────────────────────────
 
class CCConfirmView(discord.ui.View):
    def __init__(self, user_id: int, platform: str, handle: str):
        super().__init__(timeout=None)
        self.user_id  = user_id
        self.platform = platform
        self.handle   = handle
 
    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.success, emoji="✅", custom_id="cc_confirm")
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.user.guild_permissions.manage_roles:
            await interaction.response.send_message("❌ You don't have permission to do this.", ephemeral=True)
            return
 
        if self.user_id not in cc_database:
            cc_database[self.user_id] = {}
        cc_database[self.user_id][self.platform] = {"handle": self.handle, "confirmed": True}
 
        for child in self.children:
            child.disabled = True
 
        db_channel = bot.get_channel(CC_DATABASE_CHANNEL_ID)
        if db_channel:
            await db_channel.send(
                f"✅ CONFIRMED | USER_ID:{self.user_id} | PLATFORM:{self.platform} | HANDLE:{self.handle}"
            )
 
        member = interaction.guild.get_member(self.user_id)
        mention = member.mention if member else f"<@{self.user_id}>"
 
        await interaction.response.send_message(
            f"✅ Confirmed {mention}'s {self.platform} handle as `{self.handle}`!",
            ephemeral=True
        )
        await interaction.message.delete()
 
    @discord.ui.button(label="Decline", style=discord.ButtonStyle.danger, emoji="❌", custom_id="cc_decline")
    async def decline(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.user.guild_permissions.manage_roles:
            await interaction.response.send_message("❌ You don't have permission to do this.", ephemeral=True)
            return
 
        if self.user_id in cc_database:
            cc_database[self.user_id].pop(self.platform, None)
 
        for child in self.children:
            child.disabled = True
 
        db_channel = bot.get_channel(CC_DATABASE_CHANNEL_ID)
        if db_channel:
            await db_channel.send(f"❌ DECLINED | USER_ID:{self.user_id} | PLATFORM:{self.platform}")
 
        member = interaction.guild.get_member(self.user_id)
        mention = member.mention if member else f"<@{self.user_id}>"
 
        await interaction.response.send_message(
            f"❌ Declined {mention}'s {self.platform} handle submission.",
            ephemeral=True
        )
        await interaction.message.delete()
 
 
# ── /linkcc command ───────────────────────────────────────────────────────
 
@bot.tree.command(name="linkcc", description="Link your YouTube or TikTok handle (you can link both)")
@discord.app_commands.describe(
    platform="Your platform (youtube or tiktok)",
    handle="Your channel/account handle (without @)"
)
@discord.app_commands.choices(platform=[
    discord.app_commands.Choice(name="YouTube", value="youtube"),
    discord.app_commands.Choice(name="TikTok",  value="tiktok"),
])
async def linkcc(interaction: discord.Interaction, platform: str, handle: str):
    role = interaction.guild.get_role(CC_ROLE_ID)
    if not role or role not in interaction.user.roles:
        await interaction.response.send_message(
            "❌ You don't have permission to use this command.", ephemeral=True
        )
        return
 
    handle = handle.lstrip("@").strip()
 
    if interaction.user.id not in cc_database:
        cc_database[interaction.user.id] = {}
    cc_database[interaction.user.id][platform] = {"handle": handle, "confirmed": False}
 
    db_channel = bot.get_channel(CC_DATABASE_CHANNEL_ID)
    if db_channel:
        view = CCConfirmView(
            user_id=interaction.user.id,
            platform=platform,
            handle=handle
        )
        await db_channel.send(
            f"🕐 PENDING | USER_ID:{interaction.user.id} | PLATFORM:{platform} | HANDLE:{handle}\n\n"
            f"{interaction.user.mention} has submitted their {platform} handle as `{handle}`\n"
            f"An admin/mod can confirm or decline below:",
            view=view
        )
 
    await interaction.response.send_message(
        f"✅ Your `{platform}` handle `{handle}` has been submitted for review!",
        ephemeral=True
    )
 
 
# ── Stats fetchers ────────────────────────────────────────────────────────
 
async def get_youtube_stats(session: aiohttp.ClientSession, handle: str) -> dict:
    """Get count and total views of #dreamyvr videos posted in the last 30 days."""
    url = "https://www.googleapis.com/youtube/v3/channels"
    params = {"part": "id", "forHandle": handle, "key": YOUTUBE_API_KEY}
    async with session.get(url, params=params) as r:
        data = await r.json()
        items = data.get("items", [])
        if not items:
            return {"videos": 0, "views": 0}
        channel_id = items[0]["id"]
 
    since = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    url = "https://www.googleapis.com/youtube/v3/search"
    params = {
        "part": "id",
        "channelId": channel_id,
        "type": "video",
        "publishedAfter": since,
        "maxResults": 50,
        "key": YOUTUBE_API_KEY,
    }
    async with session.get(url, params=params) as r:
        data = await r.json()
        items = data.get("items", [])
        if not items:
            return {"videos": 0, "views": 0}
        video_ids = [item["id"]["videoId"] for item in items]
 
    url = "https://www.googleapis.com/youtube/v3/videos"
    params = {
        "part": "snippet,statistics",
        "id": ",".join(video_ids),
        "key": YOUTUBE_API_KEY,
    }
    async with session.get(url, params=params) as r:
        data = await r.json()
 
    total_views = 0
    dreamyvr_count = 0
 
    for item in data.get("items", []):
        snippet  = item.get("snippet", {})
        combined = f"{snippet.get('title', '')} {snippet.get('description', '')} {' '.join(snippet.get('tags', []))}"
        if not re.search(r'#dreamyvr', combined, re.IGNORECASE):
            continue
        dreamyvr_count += 1
        total_views += int(item.get("statistics", {}).get("viewCount", 0))
 
    return {"videos": dreamyvr_count, "views": total_views}
 
 
async def get_tiktok_stats(session: aiohttp.ClientSession, handle: str) -> dict:
    """Get count and total views of #dreamyvr TikToks posted in the last 30 days via RapidAPI."""
    headers = {
        "x-rapidapi-key": RAPIDAPI_KEY,
        "x-rapidapi-host": "tiktok-scraper7.p.rapidapi.com",
        "Content-Type": "application/json"
    }
 
    try:
        async with session.get(
            "https://tiktok-scraper7.p.rapidapi.com/user/info",
            headers=headers,
            params={"unique_id": handle}
        ) as r:
            user_data = await r.json()
            user_id = user_data["data"]["user"]["id"]
 
        async with session.get(
            "https://tiktok-scraper7.p.rapidapi.com/user/posts",
            headers=headers,
            params={"user_id": user_id, "count": "35", "cursor": "0", "sort_type": "0"}
        ) as r:
            posts_data = await r.json()
 
        videos = posts_data.get("data", {}).get("videos", [])
        if not videos:
            return {"videos": 0, "views": 0}
 
        cutoff = datetime.now(timezone.utc) - timedelta(days=30)
        total_views = 0
        dreamyvr_count = 0
 
        for video in videos:
            create_time = datetime.fromtimestamp(video.get("create_time", 0), tz=timezone.utc)
            if create_time < cutoff:
                continue
            if not re.search(r'#dreamyvr', video.get("title", ""), re.IGNORECASE):
                continue
            dreamyvr_count += 1
            total_views += video.get("play_count", 0)
 
        return {"videos": dreamyvr_count, "views": total_views}
 
    except Exception as e:
        print(f"TikTok RapidAPI error: {e}")
        return {"videos": 0, "views": 0}
 
 
async def get_combined_stats(session: aiohttp.ClientSession, user_id: int) -> dict:
    """Fetch and combine stats across all confirmed platforms for a user."""
    user_entry = cc_database.get(user_id, {})
    total_videos = 0
    total_views  = 0
 
    yt_entry = user_entry.get("youtube")
    if yt_entry and yt_entry["confirmed"]:
        stats = await get_youtube_stats(session, yt_entry["handle"])
        total_videos += stats["videos"]
        total_views  += stats["views"]
 
    tt_entry = user_entry.get("tiktok")
    if tt_entry and tt_entry["confirmed"]:
        stats = await get_tiktok_stats(session, tt_entry["handle"])
        total_videos += stats["videos"]
        total_views  += stats["views"]
 
    # If they hit 5+ videos, count views as full 30-day regardless of timing
    return {"videos": total_videos, "views": total_views}
 
 
def get_highest_qualifying_tier_index(videos: int, views: int) -> int:
    """Return the index of the highest tier the user qualifies for, or -1 if none."""
    highest = -1
    for i, tier in enumerate(TIERS):
        qualifies = False
        if tier["min_videos"] > 0 and videos >= tier["min_videos"]:
            qualifies = True
        if tier["min_views"] > 0 and videos >= 5 and views >= tier["min_views"]:
            qualifies = True
        if qualifies:
            highest = i
    return highest
 
 
# ── Tier check task ───────────────────────────────────────────────────────
 
@tasks.loop(seconds=CHECK_INTERVAL)
async def check_tiers():
    if not bot.guilds:
        return
    guild = bot.guilds[0]
    submissions_channel = bot.get_channel(SUBMISSIONS_CHANNEL_ID)
    if not submissions_channel:
        return
 
    print(f"Checking tiers for {len(cc_database)} linked users...")
 
    async with aiohttp.ClientSession() as session:
        for user_id, platforms in cc_database.items():
            # Skip if no confirmed platforms
            has_confirmed = any(p.get("confirmed") for p in platforms.values())
            if not has_confirmed:
                continue
 
            try:
                stats = await get_combined_stats(session, user_id)
                videos = stats["videos"]
                views  = stats["views"]
 
                current_highest = get_highest_qualifying_tier_index(videos, views)
                previously_notified = user_tier_cache.get(user_id, -1)
 
                if current_highest > previously_notified:
                    # Notify for each new tier they've unlocked
                    for tier_index in range(previously_notified + 1, current_highest + 1):
                        tier = TIERS[tier_index]
                        member = guild.get_member(user_id)
                        mention = member.mention if member else f"<@{user_id}>"
                        role_mention = f"<@&{tier['role_id']}>"
                        await submissions_channel.send(
                            f"🎉 {mention} is ready for the **{tier['label']}** tier! {role_mention}"
                        )
                        await asyncio.sleep(0.5)
 
                    user_tier_cache[user_id] = current_highest
 
                await asyncio.sleep(1)
 
            except Exception as e:
                print(f"Tier check error for user {user_id}: {e}")
 
@check_tiers.before_loop
async def before_check_tiers():
    await bot.wait_until_ready()
 
 
# ── /ccstats command ──────────────────────────────────────────────────────
 
@bot.tree.command(name="ccstats", description="See your linked channel stats for the last 30 days (#dreamyvr videos only)")
@discord.app_commands.describe(platform="Which platform to check (youtube or tiktok)")
@discord.app_commands.choices(platform=[
    discord.app_commands.Choice(name="YouTube", value="youtube"),
    discord.app_commands.Choice(name="TikTok",  value="tiktok"),
])
async def ccstats(interaction: discord.Interaction, platform: str):
    user_id = interaction.user.id
    user_entry = cc_database.get(user_id, {})
    platform_entry = user_entry.get(platform)
 
    if not platform_entry:
        await interaction.response.send_message(
            f"❌ You haven't linked a {platform} handle yet. Use `/linkcc` to add one!",
            ephemeral=True
        )
        return
 
    if not platform_entry["confirmed"]:
        await interaction.response.send_message(
            f"⏳ Your {platform} handle is still pending confirmation by an admin/mod.",
            ephemeral=True
        )
        return
 
    await interaction.response.defer(ephemeral=True)
    handle = platform_entry["handle"]
 
    async with aiohttp.ClientSession() as session:
        if platform == "youtube":
            stats = await get_youtube_stats(session, handle)
        else:
            stats = await get_tiktok_stats(session, handle)
 
    platform_label = "YouTube" if platform == "youtube" else "TikTok"
    await interaction.followup.send(
        f"📊 **Your {platform_label} stats (last 30 days, #dreamyvr videos only)**\n"
        f"Handle: `@{handle}`\n"
        f"#dreamyvr videos posted: **{stats['videos']}**\n"
        f"Total views: **{stats['views']:,}**",
        ephemeral=True
    )
 
 
# ── /linkleaderboard command ──────────────────────────────────────────────
 
@bot.tree.command(name="linkleaderboard", description="See all linked content creators")
async def linkleaderboard(interaction: discord.Interaction):
    await interaction.response.defer()
 
    if not cc_database:
        await interaction.followup.send("No linked accounts yet.")
        return
 
    lines = ["📋 **Linked Content Creators**\n"]
 
    for user_id, platforms in cc_database.items():
        confirmed_platforms = {p: d for p, d in platforms.items() if d.get("confirmed")}
        if not confirmed_platforms:
            continue
 
        member = interaction.guild.get_member(user_id)
        mention = member.mention if member else f"<@{user_id}>"
 
        platform_parts = []
        for platform, data in confirmed_platforms.items():
            emoji = "▶️" if platform == "youtube" else "🎵"
            platform_parts.append(f"{emoji} {platform.capitalize()}: `@{data['handle']}`")
 
        lines.append(f"{mention} — {' | '.join(platform_parts)}")
 
    # Split into chunks to avoid Discord's 2000 char limit
    chunks = []
    current = ""
    for line in lines:
        if len(current) + len(line) + 1 > 1900:
            chunks.append(current)
            current = line + "\n"
        else:
            current += line + "\n"
    if current:
        chunks.append(current)
 
    for chunk in chunks:
        await interaction.followup.send(chunk)
 
 
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
    params = {"part": "snippet", "id": video_id, "key": YOUTUBE_API_KEY}
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
        if not already_reacted(first_message):
            await first_message.add_reaction("👍")
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
 
 
async def send_notification(video_url: str):
    channel = bot.get_channel(DISCORD_CHANNEL_ID)
    if channel:
        await channel.send(
            f"Hey everyone, we just posted a video! Go check it out!\n"
            f"{ROLE_MENTION}\n"
            f"{video_url}"
        )
 
 
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
 
    bot.add_view(CCConfirmView(user_id=0, platform="youtube", handle=""))
 
    await load_cc_database()
 
    for guild in bot.guilds:
        await update_member_count(guild)
 
    check_socials.start()
    check_forum.start()
    check_tiers.start()
 
 
bot.run(DISCORD_TOKEN)
