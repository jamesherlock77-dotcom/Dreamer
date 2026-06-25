import discord
from discord.ext import commands, tasks
from discord import app_commands
import os
import re
import json
import io
import asyncio
import aiohttp
from datetime import datetime, time, timedelta
import pytz
from typing import Literal

# ── Config ───────────────────────────────────────────────────────────────────
TOKEN              = os.environ["DISCORD_BOT_TOKEN"]
YOUTUBE_API_KEY    = "AIzaSyAe5hyEAwxTCdBbZRQQsGfuQC6xlQWUBg0"
KEYAPI_TOKEN       = "46d0ce4df7bc41c0a9a62675f0e4231d"   # EchoTik / KeyAPI bearer token
KEYAPI_BASE        = "https://api.keyapi.ai"
DB_CHANNEL_ID      = 1515064641246466113
LINK_CMD_CHANNEL   = 1513272619439226980
LINK_LOG_CHANNEL   = 1512899799077093546
RESET_TIME         = time(23, 0)
RESET_WEEKDAY      = 6
TIMEZONE           = pytz.timezone("UTC")

# ── Weekly leaderboard winners config ─────────────────────────────────────────
LEADERBOARD_WINNERS_CHANNEL = 1495873647775322202
LEADERBOARD_WINNER_ROLE_ID  = 1515066635667505323
TOP_N_WINNERS                = 5

# ── Video announcement config ─────────────────────────────────────────────────
VIDEO_ANNOUNCE_CHANNEL  = 1512853017920143560
VIDEO_ANNOUNCE_ROLE_ID  = 1512854249174863882
VIDEO_STATE_CHANNEL_ID  = 1495388852020445255
TIKTOK_USERNAME          = "dreamyvrofficial"
YOUTUBE_HANDLE           = "dreamyvrofficial"
VIDEO_CHECK_INTERVAL_MIN = 10

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
MESSAGES_REQUIRED   = 3
STREAK_WINDOW_HOURS = 24
STREAK_GRACE_HOURS  = 4

# ── Mod rewards config ───────────────────────────────────────────────────────
MOD_ROLE_ID         = 1423121100358811795
MOD_PTS_DB_CHANNEL  = 1516176492663537875
DYNO_BOT_ID         = 155149108183695360
DYNO_PREFIXES       = ("?", "!", ".")
PTS_BAN             = 3
PTS_WARN            = 1
PTS_THANKS          = 2
PTS_PER_50_MSGS     = 2
MSG_MILESTONE       = 50

_staff_msg_counts: dict[str, int] = {}
THANKS_PATTERNS = re.compile(r"\b(thanks?|thank\s*you|ty|thx|tysm)\b", re.IGNORECASE)

# ── Cache ────────────────────────────────────────────────────────────────────
_approved_links_cache: dict | None = None
_approved_links_cache_time: float  = 0
CACHE_TTL = 300

# ── Ticket system config ──────────────────────────────────────────────────────
TICKET_PANEL_CHANNEL    = 1495162997734117386
SUPPORT_ROLE_ID         = 1495495210422112366
TICKET_LOG_CHANNEL_ID   = 1517621119425839154
MOD_NOTIFS_CHANNEL_ID   = 1423121107057246239

_ticket_counter = 0
_ticket_counter_loaded = False

# ── Level forum-post config ───────────────────────────────────────────────────
LEVEL_FORUM_CHANNEL_ID = 1512556161205928046
LEVEL_ROLE_ID          = 1423121100421861438
LEVEL_ONE_TAG_ID       = 1517631235692957736
LEVEL_TWO_TAG_ID       = 1517631552241139723
LEVEL_THREE_TAG_ID     = 1517631604309098709
LEVEL_FOUR_TAG_ID      = 1517631633073766462

LEVEL_IMAGES_DIR = "."

LEVELS = {
    1: {
        "title": "Level 1",
        "content": (
            "A level that looks peaceful, but a monster is hunting you. The bright walls "
            "and decorations make the level seem harmless. Find a key to unlock a keypad. "
            "Notes are scattered throughout the level, each having a different code. Only "
            "one of the codes are correct. Find the right code to escape and move onto the "
            "🏠🌈\n\n"
            "**What to do:**\n"
            "* 🔑 Find the key to unlock the keypad\n"
            "* 📝 Find notes that have possible codes\n"
            "* 🔍 Determine the correct code\n"
            "* 🔢 Enter the correct code and escape"
        ),
        "images": ["level1_1.png", "level1_2.png"],
        "tag_id": LEVEL_ONE_TAG_ID,
    },
    2: {
        "title": "Level 2",
        "content": (
            "A peaceful looking level, but many fast monsters are trying to stop you from "
            "escaping. Search the houses to find all 5 hidden keys and unlock your way out "
            "of the level.\n\n"
            "**What to do:**\n"
            "* 🔑 Search the houses for 5 keys\n"
            "* 🌻 Run away from the monsters\n"
            "* 🌈 Enjoy the peaceful views 🥹"
        ),
        "images": ["level2_1.png", "level2_2.png"],
        "tag_id": LEVEL_TWO_TAG_ID,
    },
    3: {
        "title": "Level 3",
        "content": (
            "A calm, quiet facility... but something feels off. Rows of old computers hum "
            "under dim lights. To escape, you'll need to figure out what they're hiding.\n\n"
            "**What to do:**\n"
            "* 💻 Explore the room full of computers\n"
            "* 🔒 Some computer has 3 possible codes — only one is correct\n"
            "* ⌨️ Type the right code to disable the computer\n"
            "* 🚫 Disable all computers to unlock a secret door behind spawn\n"
            "* 🔑 Grab the key from behind the door\n"
            "* 🔢 Use the key at the exit keyhole to escape"
        ),
        "images": ["level3_1.png", "level3_2.png"],
        "tag_id": LEVEL_THREE_TAG_ID,
    },
    4: {
        "title": "Level 4",
        "content": (
            "A vast, tiled space stretches out — still, silent, and strangely endless. "
            "Water lies still below walkways, while heavy columns rise toward glowing "
            "overhead lights. Every corner feels familiar, yet wrong; the air holds a "
            "quiet, heavy pressure. To leave, you must follow the path hidden within its "
            "design.\n\n"
            "**What to do:**\n"
            "* ⚙️ Find and pull every lever you see — each one unlocks a new gate\n"
            "* 🚪 Gates open into new sections, each holding more levers to activate\n"
            "* 🔓 Pull them all, and the final barrier will slowly lift\n"
            "* 🚶 Beyond it waits the only door — your way out, and your escape"
        ),
        "images": ["level4_1.png", "level4_2.png"],
        "tag_id": LEVEL_FOUR_TAG_ID,
    },
}

# ── Bot setup ─────────────────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

# ── Mod rewards helpers ──────────────────────────────────────────────────────
def _is_mod(member) -> bool:
    if not member or not hasattr(member, "roles"):
        return False
    return any(r.id == MOD_ROLE_ID for r in member.roles)

async def _award_points(uid: int, points: int, reason: str):
    db = bot.get_channel(MOD_PTS_DB_CHANNEL)
    if db:
        await db.send(f"MODPTS:{uid}:{points}:{reason}")

async def _tally_mod_points() -> dict:
    db = bot.get_channel(MOD_PTS_DB_CHANNEL)
    result: dict = {}
    if not db:
        return result
    async for msg in db.history(limit=None, oldest_first=True):
        if not msg.author.bot:
            continue
        if not msg.content.startswith("MODPTS:"):
            continue
        parts = msg.content.split(":", 3)
        if len(parts) < 4:
            continue
        _, uid, pts_str, reason = parts
        try:
            pts = int(pts_str)
        except ValueError:
            continue
        entry = result.setdefault(uid, {"total": 0, "breakdown": {}})
        entry["total"] += pts
        entry["breakdown"][reason] = entry["breakdown"].get(reason, 0) + pts
    return result

async def _find_dyno_invoker(channel: discord.TextChannel, keyword: str):
    try:
        async for m in channel.history(limit=25):
            if m.author.bot:
                continue
            content = (m.content or "").lower()
            if keyword not in content:
                continue
            if content.startswith(DYNO_PREFIXES) or content.startswith(f"/{keyword}"):
                return m.author
    except (discord.Forbidden, discord.HTTPException):
        pass
    return None

async def handle_dyno_warn(message: discord.Message):
    if message.author.id != DYNO_BOT_ID or not message.guild:
        return
    text_blobs = [message.content or ""]
    for e in message.embeds:
        if e.title:       text_blobs.append(e.title)
        if e.description: text_blobs.append(e.description)
        for f in e.fields:
            text_blobs.append(f.name or "")
            text_blobs.append(f.value or "")
        if e.footer and e.footer.text:
            text_blobs.append(e.footer.text)
        if e.author and e.author.name:
            text_blobs.append(e.author.name)
    combined = " ".join(text_blobs).lower()
    if "warn" not in combined:
        return

    invoker_id = None
    id_re = re.compile(r"(\d{17,20})")
    for e in message.embeds:
        candidates = []
        for f in e.fields:
            if "mod" in (f.name or "").lower():
                candidates.append(f.value or "")
        if e.footer and e.footer.text and "mod" in e.footer.text.lower():
            candidates.append(e.footer.text)
        for c in candidates:
            m = id_re.search(c)
            if m:
                invoker_id = int(m.group(1))
                break
        if invoker_id:
            break

    if not invoker_id:
        invoker = await _find_dyno_invoker(message.channel, "warn")
        if invoker:
            invoker_id = invoker.id

    if not invoker_id:
        return
    member = message.guild.get_member(invoker_id)
    if not _is_mod(member):
        return
    await _award_points(invoker_id, PTS_WARN, "warn")

# ── Track messages to prevent double points for "thanks" ─────────────────────
_thanked_messages: set[int] = set()
_thank_cooldowns: dict[str, str] = {}

async def handle_mod_rewards(message: discord.Message):
    if message.author.bot or not message.guild:
        return
    author_member = message.guild.get_member(message.author.id)

    if _is_mod(author_member):
        uid = str(message.author.id)
        _staff_msg_counts[uid] = _staff_msg_counts.get(uid, 0) + 1
        if _staff_msg_counts[uid] >= MSG_MILESTONE:
            _staff_msg_counts[uid] = 0
            await _award_points(message.author.id, PTS_PER_50_MSGS, "messages")

    if not _is_mod(author_member) and THANKS_PATTERNS.search(message.content or ""):
        if message.id in _thanked_messages:
            return

        now = datetime.now(pytz.UTC)
        targets = []

        for m in message.mentions:
            mem = message.guild.get_member(m.id)
            if _is_mod(mem) and mem.id != message.author.id:
                last_thank_time_str = _thank_cooldowns.get(str(m.id))
                if last_thank_time_str:
                    last_time = datetime.fromisoformat(last_thank_time_str)
                    if (now - last_time) < timedelta(minutes=20):
                        continue
                _thank_cooldowns[str(m.id)] = now.isoformat()
                targets.append(mem)

        if targets:
            _thanked_messages.add(message.id)
            for staff in targets:
                await _award_points(staff.id, PTS_THANKS, "thanks")

# ── KeyAPI / EchoTik helpers ──────────────────────────────────────────────────
async def _keyapi_get(session: aiohttp.ClientSession, path: str, params: dict = {}) -> dict:
    async with session.get(
        f"{KEYAPI_BASE}/{path.lstrip('/')}",
        headers={"Authorization": f"Bearer {KEYAPI_TOKEN}", "Accept": "application/json"},
        params=params,
    ) as r:
        raw = await r.text()
        if r.status != 200:
            raise ValueError(f"KeyAPI returned status {r.status}: {raw[:200]}")
        try:
            return json.loads(raw)
        except json.JSONDecodeError as e:
            raise ValueError(f"KeyAPI returned non-JSON: {raw[:200]}") from e

# ── Video announcement helpers ────────────────────────────────────────────────
_last_video_ids: dict[str, str] = {}
_video_ids_loaded = False

async def _load_last_video_ids():
    global _video_ids_loaded
    if _video_ids_loaded:
        return
    state_channel = bot.get_channel(VIDEO_STATE_CHANNEL_ID)
    if not state_channel:
        _video_ids_loaded = True
        return
    async for msg in state_channel.history(limit=None, oldest_first=True):
        if not msg.author.bot:
            continue
        if not msg.content.startswith("LASTVIDEO:"):
            continue
        parts = msg.content.split(":", 2)
        if len(parts) != 3:
            continue
        _, platform, video_id = parts
        _last_video_ids[platform] = video_id
    _video_ids_loaded = True

async def _save_last_video_id(platform: str, video_id: str):
    _last_video_ids[platform] = video_id
    state_channel = bot.get_channel(VIDEO_STATE_CHANNEL_ID)
    if state_channel:
        await state_channel.send(f"LASTVIDEO:{platform}:{video_id}")

async def get_latest_tiktok_video(username: str):
    """Returns (video_id, video_url) for the most recent post, or (None, None)."""
    async with aiohttp.ClientSession() as session:
        try:
            data = await _keyapi_get(session, "tiktok/influencer/videos", {"username": username, "count": 1})
        except ValueError:
            return None, None
        result = data.get("data", {})
        videos = result.get("videos") if isinstance(result, dict) else result
        if not videos:
            return None, None
        video    = videos[0]
        video_id = str(video.get("id") or video.get("video_id") or "")
        if not video_id:
            return None, None
        return video_id, f"https://www.tiktok.com/@{username}/video/{video_id}"

async def get_latest_youtube_video(handle: str):
    """Returns (video_id, video_url) for the most recent upload, or (None, None)."""
    base = "https://www.googleapis.com/youtube/v3"
    async with aiohttp.ClientSession() as session:
        params = {"part": "id", "forHandle": f"@{handle}", "key": YOUTUBE_API_KEY}
        async with session.get(f"{base}/channels", params=params) as r:
            data = await r.json()
        items = data.get("items", [])
        if not items:
            return None, None
        channel_id = items[0]["id"]

        search_params = {
            "part": "id", "channelId": channel_id, "type": "video",
            "order": "date", "maxResults": 1, "key": YOUTUBE_API_KEY,
        }
        async with session.get(f"{base}/search", params=search_params) as r:
            sdata = await r.json()
        sitems = sdata.get("items", [])
        if not sitems:
            return None, None
        video_id = sitems[0].get("id", {}).get("videoId")
        if not video_id:
            return None, None
        return video_id, f"https://www.youtube.com/watch?v={video_id}"

async def _announce_video(video_url: str):
    channel = bot.get_channel(VIDEO_ANNOUNCE_CHANNEL)
    if not channel:
        return
    content = (
        "Hey everyone, we just posted a video! Go check it out!\n"
        f"<@&{VIDEO_ANNOUNCE_ROLE_ID}>\n"
        f"{video_url}"
    )
    await channel.send(content, allowed_mentions=discord.AllowedMentions(roles=True))

@tasks.loop(minutes=VIDEO_CHECK_INTERVAL_MIN)
async def video_checker():
    await _load_last_video_ids()

    try:
        tiktok_id, tiktok_url = await get_latest_tiktok_video(TIKTOK_USERNAME)
        if tiktok_id and tiktok_id != _last_video_ids.get("tiktok"):
            is_first_check = "tiktok" not in _last_video_ids
            await _save_last_video_id("tiktok", tiktok_id)
            if not is_first_check:
                await _announce_video(tiktok_url)
    except Exception as e:
        print(f"[video_checker] TikTok check failed: {e}")

    try:
        yt_id, yt_url = await get_latest_youtube_video(YOUTUBE_HANDLE)
        if yt_id and yt_id != _last_video_ids.get("youtube"):
            is_first_check = "youtube" not in _last_video_ids
            await _save_last_video_id("youtube", yt_id)
            if not is_first_check:
                await _announce_video(yt_url)
    except Exception as e:
        print(f"[video_checker] YouTube check failed: {e}")

# ── Log every message into the DB channel ────────────────────────────────────
@bot.event
async def on_message(message: discord.Message):
    await handle_dyno_warn(message)

    if message.author.bot:
        return
    if message.channel.id in (DB_CHANNEL_ID, 1500327292830875898, STREAK_DB_CHANNEL, MOD_PTS_DB_CHANNEL):
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
    await handle_mod_rewards(message)
    await bot.process_commands(message)

# ── Ban detection ────────────────────────────────────────────────────────────
@bot.event
async def on_member_ban(guild: discord.Guild, user):
    try:
        async for entry in guild.audit_logs(limit=5, action=discord.AuditLogAction.ban):
            if entry.target and entry.target.id == user.id:
                mod = entry.user
                if mod and not mod.bot and _is_mod(guild.get_member(mod.id)):
                    await _award_points(mod.id, PTS_BAN, "ban")
                break
    except discord.Forbidden:
        pass

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

async def get_approved_link(user_id: int, platform: str = None):
    cache = await _build_links_cache()
    entries = cache.get(str(user_id))
    if not entries:
        return None, None
    if isinstance(entries, list):
        if platform:
            for e in entries:
                if e["platform"] == platform:
                    return e["platform"], e["url"]
        return entries[0]["platform"], entries[0]["url"]
    if platform and entries["platform"] != platform:
        return None, None
    return entries["platform"], entries["url"]

async def get_all_approved_links() -> list:
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
            params = {"part": "id,snippet", "forHandle": f"@{value}", "key": YOUTUBE_API_KEY}
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
        else:
            params = {"part": "id,snippet", "forUsername": value, "key": YOUTUBE_API_KEY}
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

        params = {"part": "snippet,statistics", "id": channel_id, "key": YOUTUBE_API_KEY}
        async with session.get(f"{base}/channels", params=params) as r:
            data = await r.json()
        items = data.get("items", [])
        if not items:
            raise ValueError("Could not fetch YouTube channel stats.")
        ch      = items[0]
        stats   = ch.get("statistics", {})
        snippet = ch.get("snippet", {})

        dreamyvr_views = 0
        dreamyvr_count = 0
        next_page_token = None
        while True:
            search_params = {
                "part": "id", "channelId": channel_id, "q": "#dreamyvr",
                "type": "video", "maxResults": 50, "key": YOUTUBE_API_KEY,
            }
            if next_page_token:
                search_params["pageToken"] = next_page_token
            async with session.get(f"{base}/search", params=search_params) as sr:
                sdata = await sr.json()
            video_ids = [i["id"]["videoId"] for i in sdata.get("items", []) if "videoId" in i.get("id", {})]
            if video_ids:
                stats_params = {"part": "statistics", "id": ",".join(video_ids), "key": YOUTUBE_API_KEY}
                async with session.get(f"{base}/videos", params=stats_params) as vr:
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

# ── TikTok helpers (KeyAPI / EchoTik) ────────────────────────────────────────
def extract_tiktok_username(url: str):
    m = re.search(r"tiktok\.com/@([A-Za-z0-9_\.]+)", url)
    return m.group(1) if m else None

async def fetch_tiktok_posts_data(username: str):
    """Returns (total_dreamyvr_views, dreamyvr_video_count, follower_count)."""
    total_views    = 0
    video_count    = 0
    follower_count = 0

    async with aiohttp.ClientSession() as session:
        # Fetch follower count from profile
        try:
            profile_data   = await _keyapi_get(session, "tiktok/influencer/detail", {"username": username})
            follower_count = int(profile_data.get("data", {}).get("followers", 0))
        except ValueError:
            pass

        # Paginate through all videos, filter for #dreamyvr
        cursor   = None
        has_more = True
        while has_more:
            params = {"username": username, "count": 30}
            if cursor:
                params["cursor"] = cursor
            try:
                data = await _keyapi_get(session, "tiktok/influencer/videos", params)
            except ValueError:
                break

            result   = data.get("data", {})
            videos   = result.get("videos") if isinstance(result, dict) else (result if isinstance(result, list) else [])
            has_more = bool(result.get("has_more", False)) if isinstance(result, dict) else False
            cursor   = result.get("cursor") if isinstance(result, dict) else None

            if not videos:
                break

            for video in videos:
                desc       = (video.get("desc") or video.get("title") or video.get("description") or "").lower()
                # Also check hashtag lists if the API returns them
                challenges = [c.get("title", "").lower() for c in video.get("challenges", [])]
                text_extra = [t.get("hashtagName", "").lower() for t in video.get("textExtra", [])]
                has_tag    = "dreamyvr" in desc or "dreamyvr" in challenges or "dreamyvr" in text_extra
                if has_tag:
                    play_count  = (
                        video.get("play_count")
                        or video.get("views")
                        or video.get("playCount")
                        or video.get("stats", {}).get("playCount")
                        or 0
                    )
                    total_views += int(play_count)
                    video_count += 1

            if not has_more or not cursor:
                break

    return total_views, video_count, follower_count

async def fetch_tiktok_stats(url: str):
    username = extract_tiktok_username(url)
    if not username:
        raise ValueError("Couldn't parse that TikTok URL.")
    async with aiohttp.ClientSession() as session:
        try:
            profile_data = await _keyapi_get(session, "tiktok/influencer/detail", {"username": username})
            nickname     = profile_data.get("data", {}).get("nickname", username)
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

# ── /modstats ────────────────────────────────────────────────────────────────
@tree.command(name="modstats", description="View moderator points (staff only)")
@app_commands.describe(user="Optional: view another mod's stats")
async def modstats(interaction: discord.Interaction, user: discord.Member = None):
    if not _is_mod(interaction.user):
        await interaction.response.send_message("You don't have permission to use this command.", ephemeral=True)
        return
    await interaction.response.defer()
    target = user or interaction.user
    tally  = await _tally_mod_points()
    entry  = tally.get(str(target.id), {"total": 0, "breakdown": {}})
    bd     = entry["breakdown"]
    embed = discord.Embed(
        title=f"Mod Stats — {target.display_name}",
        color=0x5865F2,
    )
    embed.set_thumbnail(url=target.display_avatar.url)
    embed.add_field(name="Total Points", value=f"`{entry['total']}`", inline=False)
    embed.add_field(name="Bans (×3)",     value=f"`{bd.get('ban', 0)}`",      inline=True)
    embed.add_field(name="Warns (×1)",    value=f"`{bd.get('warn', 0)}`",     inline=True)
    embed.add_field(name="Thanks (×2)",   value=f"`{bd.get('thanks', 0)}`",   inline=True)
    embed.add_field(name="Messages (×2/50)", value=f"`{bd.get('messages', 0)}`", inline=True)
    embed.set_footer(text=f"Requested by {interaction.user}")
    await interaction.followup.send(embed=embed)

# ── /modleaderboard ──────────────────────────────────────────────────────────
@tree.command(name="modleaderboard", description="Top moderators by points (staff only)")
async def modleaderboard(interaction: discord.Interaction):
    if not _is_mod(interaction.user):
        await interaction.response.send_message("You don't have permission to use this command.", ephemeral=True)
        return
    await interaction.response.defer()
    tally = await _tally_mod_points()
    if not tally:
        await interaction.followup.send("No mod points tracked yet.")
        return
    ranked = sorted(tally.items(), key=lambda kv: kv[1]["total"], reverse=True)[:10]
    lines = ["**__Staff Point Leaderboard__**"]
    for i, (uid, data) in enumerate(ranked):
        member = interaction.guild.get_member(int(uid))
        name   = member.mention if member else f"<@{uid}>"
        points = data['total']
        lines.append(f"> **{i + 1}.** {name} - {points} mod points")
    embed = discord.Embed(
        title="__Staff Point Leaderboard__",
        description="\n".join(lines),
        color=0x808080,
    )
    await interaction.followup.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())

# ── /sendlevel ────────────────────────────────────────────────────────────────
def _has_level_role(member: discord.Member) -> bool:
    if not member or not hasattr(member, "roles"):
        return False
    return any(r.id == LEVEL_ROLE_ID for r in member.roles)

@tree.command(name="sendlevel", description="Post a level guide to the forum (restricted)")
@app_commands.describe(level="Which level to post")
async def sendlevel(interaction: discord.Interaction, level: Literal[1, 2, 3, 4]):
    member = interaction.guild.get_member(interaction.user.id) if interaction.guild else None
    if not _has_level_role(member):
        await interaction.response.send_message(
            "You don't have permission to use this command.", ephemeral=True)
        return

    level_data = LEVELS.get(level)
    if not level_data:
        await interaction.response.send_message("That level isn't configured yet.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    forum_channel = bot.get_channel(LEVEL_FORUM_CHANNEL_ID)
    if not forum_channel or not isinstance(forum_channel, discord.ForumChannel):
        await interaction.followup.send("Could not reach the forum channel.", ephemeral=True)
        return

    applied_tags = []
    tag_id = level_data.get("tag_id")
    if tag_id:
        tag = discord.utils.get(forum_channel.available_tags, id=tag_id)
        if tag:
            applied_tags.append(tag)

    files = []
    for filename in level_data.get("images", []):
        path = os.path.join(LEVEL_IMAGES_DIR, filename)
        if os.path.isfile(path):
            files.append(discord.File(path, filename=filename))

    try:
        thread_with_message = await forum_channel.create_thread(
            name=level_data["title"],
            content=level_data["content"],
            applied_tags=applied_tags,
            files=files if files else discord.utils.MISSING,
        )
    except discord.HTTPException as e:
        await interaction.followup.send(f"Failed to create the forum post: {e}", ephemeral=True)
        return

    thread = thread_with_message.thread
    await interaction.followup.send(f"Posted: {thread.mention}", ephemeral=True)

@tree.command(name="test", description="Test command (restricted)")
async def test(interaction: discord.Interaction):
    member = interaction.guild.get_member(interaction.user.id) if interaction.guild else None
    if not _has_level_role(member):
        await interaction.response.send_message(
            "You don't have permission to use this command.", ephemeral=True)
        return
    await interaction.response.send_message("✅ Test command works!")

# ── Weekly reset ──────────────────────────────────────────────────────────────
@tasks.loop(time=RESET_TIME)
async def weekly_reset():
    now = datetime.now(TIMEZONE)
    if now.weekday() != RESET_WEEKDAY:
        return
    db_channel = bot.get_channel(DB_CHANNEL_ID)
    if not db_channel:
        return

    counts = await tally_counts()
    guild = db_channel.guild
    if counts and guild:
        sorted_users = sorted(counts.items(), key=lambda x: x[1], reverse=True)[:TOP_N_WINNERS]
        lines = []
        winner_role = guild.get_role(LEADERBOARD_WINNER_ROLE_ID)

        if winner_role:
            for member in list(winner_role.members):
                try:
                    await member.remove_roles(winner_role)
                except discord.HTTPException:
                    pass

        for i, (uid, count) in enumerate(sorted_users):
            member = guild.get_member(int(uid))
            name   = member.mention if member else f"<@{uid}>"
            lines.append(f"**{i + 1}.** {name} — `{count}` msgs")
            if member and winner_role:
                try:
                    await member.add_roles(winner_role)
                except discord.HTTPException:
                    pass

        announce_channel = bot.get_channel(LEADERBOARD_WINNERS_CHANNEL)
        if announce_channel and lines:
            content = (
                "🏆 **Weekly Message Leaderboard Winners**\n\n"
                + "\n".join(lines)
                + "\n\nCongrats to this week's most active members!"
            )
            await announce_channel.send(content)

    deleted = await db_channel.purge(limit=None)
    print(f"[{now}] Weekly reset — deleted {len(deleted)} log entries.")


# ── Streaks ──────────────────────────────────────────────────────────────────
streak_data: dict = {}
_streaks_ready = asyncio.Event()
_streak_locks: dict[str, asyncio.Lock] = {}

def _get_streak_lock(uid: str) -> asyncio.Lock:
    lock = _streak_locks.get(uid)
    if lock is None:
        lock = asyncio.Lock()
        _streak_locks[uid] = lock
    return lock

async def _save_streak(uid: str):
    db = bot.get_channel(STREAK_DB_CHANNEL)
    if not db:
        return
    payload = json.dumps(streak_data[uid])
    await db.send(f"STREAK:{uid}:{payload}")

async def _load_streaks():
    db = bot.get_channel(STREAK_DB_CHANNEL)
    if not db:
        return
    latest = {}
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

def _get_streak(uid: str) -> dict:
    return streak_data.get(uid, {
        "streak":          0,
        "messages_today":  0,
        "last_streak_time": None,
        "pending":         False,
    })

async def _update_streak_roles(member: discord.Member, streak: int):
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

@tasks.loop(minutes=5)
async def streak_checker():
    if not _streaks_ready.is_set():
        return
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    to_break = []
    for uid, data in streak_data.items():
        if not data.get("last_streak_time"):
            continue
        last = datetime.fromisoformat(data["last_streak_time"])
        elapsed = (now - last).total_seconds() / 3600
        window_end = STREAK_WINDOW_HOURS + STREAK_GRACE_HOURS
        if elapsed >= window_end and data.get("streak", 0) > 0:
            to_break.append(uid)
    for uid in to_break:
        data = streak_data[uid]
        old_streak = data["streak"]
        data["streak"]         = 0
        data["messages_today"] = 0
        data["pending"]        = False
        await _save_streak(uid)
        announce = bot.get_channel(STREAK_ANNOUNCE_CHANNEL)
        if announce:
            await announce.send(f"💔 <@{uid}>, you have lost your streak of `{old_streak}`!")
        for guild in bot.guilds:
            member = guild.get_member(int(uid))
            if member:
                await _remove_all_streak_roles(member)

async def handle_streak(message: discord.Message):
    from datetime import datetime, timezone
    await _streaks_ready.wait()

    uid = str(message.author.id)
    async with _get_streak_lock(uid):
        now  = datetime.now(timezone.utc)
        data = _get_streak(uid)
        last_time = datetime.fromisoformat(data["last_streak_time"]) if data.get("last_streak_time") else None
        elapsed_hours = (now - last_time).total_seconds() / 3600 if last_time else None
        if elapsed_hours is not None and elapsed_hours > STREAK_WINDOW_HOURS + STREAK_GRACE_HOURS:
            data["messages_today"] = 0
        data["messages_today"] = data.get("messages_today", 0) + 1
        streak_data[uid] = data
        if data["messages_today"] >= MESSAGES_REQUIRED:
            in_window   = last_time is not None and elapsed_hours >= STREAK_WINDOW_HOURS
            no_streak_yet = last_time is None or data.get("streak", 0) == 0
            if in_window or no_streak_yet:
                data["streak"] = data.get("streak", 0) + 1
                data["messages_today"] = 0
                data["last_streak_time"] = now.isoformat()
                streak_data[uid] = data
                await _save_streak(uid)
                announce = bot.get_channel(STREAK_ANNOUNCE_CHANNEL)
                if announce:
                    await announce.send(
                        f"<:Sneeze:1495243609035899023> <@{uid}>, you have acquired a chat streak!\n"
                        f"**Streak:** `{data['streak']}`"
                    )
                member = message.guild.get_member(int(message.author.id))
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
        await channel.edit(name=f"☁️・Members: {guild.member_count}")

@bot.event
async def on_member_join(member: discord.Member):
    await update_member_count(member.guild)

@bot.event
async def on_member_remove(member: discord.Member):
    await update_member_count(member.guild)

# ═════════════════════════════════════════════════════════════════════════════
# ── Ticket system ─────────────────────────────────────────────────────────────
# ═════════════════════════════════════════════════════════════════════════════

async def _load_ticket_counter():
    global _ticket_counter, _ticket_counter_loaded
    if _ticket_counter_loaded:
        return
    log_channel = bot.get_channel(TICKET_LOG_CHANNEL_ID)
    if not log_channel:
        _ticket_counter_loaded = True
        return
    last_value = 0
    async for msg in log_channel.history(limit=None, oldest_first=True):
        if msg.author.bot and msg.content.startswith("TICKETNUM:"):
            try:
                last_value = int(msg.content.split(":", 1)[1])
            except (ValueError, IndexError):
                continue
    _ticket_counter = last_value
    _ticket_counter_loaded = True

async def _next_ticket_number() -> int:
    global _ticket_counter
    await _load_ticket_counter()
    _ticket_counter += 1
    log_channel = bot.get_channel(TICKET_LOG_CHANNEL_ID)
    if log_channel:
        await log_channel.send(f"TICKETNUM:{_ticket_counter}")
    return _ticket_counter

def _format_ticket_number(n: int) -> str:
    return str(n).zfill(4)

# ── Modals ─────────────────────────────────────────────────────────────────
class InGameSupportModal(discord.ui.Modal, title="In-game Support"):
    about_user = discord.ui.TextInput(
        label="Is your issue about another in-game user?",
        required=True,
        max_length=100,
    )
    needs = discord.ui.TextInput(
        label="What do you need?",
        style=discord.TextStyle.paragraph,
        placeholder="Describe what you need",
        required=True,
        max_length=1000,
    )

    async def on_submit(self, interaction: discord.Interaction):
        answers = {
            "Is your issue about another in-game user?": str(self.about_user),
            "What do you need?": str(self.needs),
        }
        await create_ticket(interaction, "In-game Support", answers)


class DiscordSupportModal(discord.ui.Modal, title="Discord Support"):
    about_user = discord.ui.TextInput(
        label="Is your issue about another Discord user?",
        required=True,
        max_length=100,
    )
    needs = discord.ui.TextInput(
        label="What do you need?",
        style=discord.TextStyle.paragraph,
        placeholder="Describe what you need",
        required=True,
        max_length=1000,
    )

    async def on_submit(self, interaction: discord.Interaction):
        answers = {
            "Is your issue about another Discord user?": str(self.about_user),
            "What do you need?": str(self.needs),
        }
        await create_ticket(interaction, "Discord Support", answers)


# ── Ticket creation ──────────────────────────────────────────────────────────
async def create_ticket(interaction: discord.Interaction, category: str, answers: dict):
    await interaction.response.defer(ephemeral=True)

    panel_channel = bot.get_channel(TICKET_PANEL_CHANNEL)
    if not panel_channel:
        await interaction.followup.send("Could not reach the ticket channel.", ephemeral=True)
        return

    number = await _next_ticket_number()
    padded = _format_ticket_number(number)
    thread_name = f"🎫・ticket-{padded}"

    try:
        thread = await panel_channel.create_thread(
            name=thread_name,
            type=discord.ChannelType.private_thread,
            invitable=False,
        )
    except discord.HTTPException as e:
        await interaction.followup.send(f"Couldn't create your ticket: {e}", ephemeral=True)
        return

    try:
        await thread.add_user(interaction.user)
    except discord.HTTPException:
        pass

    support_role = interaction.guild.get_role(SUPPORT_ROLE_ID)

    embed = discord.Embed(title=category, color=0x2b2d31)
    for question, answer in answers.items():
        embed.add_field(name=question, value=f"`{answer}`" if answer else "—", inline=False)
    embed.set_footer(text=f"Ticket #{padded} • Opened by {interaction.user}")

    ping = f"<@&{SUPPORT_ROLE_ID}>" if support_role else ""
    content = f"## Welcome {interaction.user.mention}\n{ping}"

    await thread.send(
        content=content,
        embed=embed,
        view=TicketOpenView(),
        allowed_mentions=discord.AllowedMentions(users=True, roles=True),
    )

    await interaction.followup.send(f"Your ticket has been created: {thread.mention}", ephemeral=True)


# ── Select menu / panel ──────────────────────────────────────────────────────
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
            min_values=1, max_values=1, options=options, custom_id="ticket_select",
        )

    async def callback(self, interaction: discord.Interaction):
        if self.values[0] == "ingame_support":
            await interaction.response.send_modal(InGameSupportModal())
        else:
            await interaction.response.send_modal(DiscordSupportModal())


class TicketPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(TicketSelect())


async def post_ticket_panel():
    channel = bot.get_channel(TICKET_PANEL_CHANNEL)
    if not channel:
        print(f"[ticket panel] ERROR: could not find channel with ID {TICKET_PANEL_CHANNEL}.")
        return

    perms = channel.permissions_for(channel.guild.me)
    missing = [name for name, ok in
               [("View Channel", perms.view_channel),
                ("Send Messages", perms.send_messages),
                ("Embed Links", perms.embed_links),
                ("Manage Messages", perms.manage_messages)]
               if not ok]
    if missing:
        print(f"[ticket panel] ERROR: bot is missing permissions in #{channel}: {', '.join(missing)}")
        return

    try:
        async for msg in channel.history(limit=50):
            if msg.author == bot.user:
                await msg.delete()
    except discord.Forbidden:
        print(f"[ticket panel] ERROR: missing 'Read Message History' or 'Manage Messages' in #{channel}")
        return
    except discord.HTTPException as e:
        print(f"[ticket panel] ERROR while clearing old panel messages: {e}")

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
    try:
        await channel.send(embed=embed, view=TicketPanelView())
        print(f"[ticket panel] Posted successfully in #{channel} ({channel.id})")
    except discord.HTTPException as e:
        print(f"[ticket panel] ERROR sending panel message: {e}")


# ── Ticket controls ───────────────────────────────────────────────────────────
class TicketOpenView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Close Ticket", style=discord.ButtonStyle.red,
                        emoji="🔒", custom_id="ticket_close")
    async def close_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        thread = interaction.channel
        if not isinstance(thread, discord.Thread):
            await interaction.response.send_message("This isn't a ticket thread.", ephemeral=True)
            return

        await interaction.response.defer()

        transcript_file = await _build_transcript(thread)
        notifs_channel = bot.get_channel(MOD_NOTIFS_CHANNEL_ID)
        transcript_link = f"<#{MOD_NOTIFS_CHANNEL_ID}>"
        if notifs_channel and transcript_file:
            await notifs_channel.send(
                content=f"📄 Transcript for {thread.name} (closed by {interaction.user.mention})",
                file=transcript_file,
            )

        await thread.send(f"Ticket Closed by {interaction.user.mention}")
        msg = await thread.send(f"Transcript saved to {transcript_link}")
        try:
            await msg.edit(content=f"Transcript saved to {transcript_link}", suppress=False)
        except discord.HTTPException:
            pass

        control_embed = discord.Embed(
            description="Support team ticket controls",
            color=0x2b2d31,
        )
        await thread.send(embed=control_embed, view=TicketClosedView())

        m = re.search(r"ticket-(\d+)", thread.name)
        number = m.group(1) if m else "0000"
        try:
            await thread.edit(name=f"Closed-{number}")
        except discord.HTTPException:
            pass

        opener_id = await _get_ticket_opener_id(thread)
        if opener_id:
            try:
                opener = await interaction.guild.fetch_member(opener_id)
                await thread.remove_user(opener)
            except (discord.HTTPException, discord.NotFound):
                pass


async def _get_ticket_opener_id(thread: discord.Thread):
    async for msg in thread.history(limit=20, oldest_first=True):
        if msg.mentions:
            return msg.mentions[0].id
    return None


async def _build_transcript(thread: discord.Thread) -> discord.File | None:
    lines = [f"Transcript for #{thread.name}", "=" * 40, ""]
    async for msg in thread.history(limit=None, oldest_first=True):
        ts = msg.created_at.strftime("%Y-%m-%d %H:%M:%S UTC")
        author = f"{msg.author} ({msg.author.id})"
        content = msg.content or ""
        if msg.embeds:
            for e in msg.embeds:
                if e.title:
                    content += f"\n[Embed: {e.title}]"
                for f in e.fields:
                    content += f"\n  {f.name}: {f.value}"
        lines.append(f"[{ts}] {author}: {content}")
    buffer = io.BytesIO("\n".join(lines).encode("utf-8"))
    return discord.File(buffer, filename=f"{thread.name}-transcript.txt")


class TicketClosedView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Transcript", style=discord.ButtonStyle.secondary,
                        emoji="📄", custom_id="ticket_transcript")
    async def transcript(self, interaction: discord.Interaction, button: discord.ui.Button):
        thread = interaction.channel
        if not isinstance(thread, discord.Thread):
            await interaction.response.send_message("This isn't a ticket thread.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        transcript_file = await _build_transcript(thread)
        if transcript_file:
            await interaction.followup.send(file=transcript_file, ephemeral=True)

    @discord.ui.button(label="Open", style=discord.ButtonStyle.secondary,
                        emoji="🔓", custom_id="ticket_open")
    async def reopen(self, interaction: discord.Interaction, button: discord.ui.Button):
        thread = interaction.channel
        if not isinstance(thread, discord.Thread):
            await interaction.response.send_message("This isn't a ticket thread.", ephemeral=True)
            return
        m = re.search(r"Closed-(\d+)", thread.name)
        number = m.group(1) if m else "0000"
        try:
            await thread.edit(name=f"🎫・ticket-{number}")
        except discord.HTTPException:
            pass
        opener_id = await _get_ticket_opener_id(thread)
        if opener_id:
            try:
                opener = await interaction.guild.fetch_member(opener_id)
                await thread.add_user(opener)
            except (discord.HTTPException, discord.NotFound):
                pass
        await interaction.response.send_message(f"🔓 Ticket reopened by {interaction.user.mention}")

    @discord.ui.button(label="Delete", style=discord.ButtonStyle.danger,
                        emoji="⛔", custom_id="ticket_delete")
    async def delete(self, interaction: discord.Interaction, button: discord.ui.Button):
        thread = interaction.channel
        if not isinstance(thread, discord.Thread):
            await interaction.response.send_message("This isn't a ticket thread.", ephemeral=True)
            return
        await interaction.response.send_message("🗑️ Deleting ticket...", ephemeral=True)
        try:
            await thread.delete()
        except discord.HTTPException:
            pass


# ── /debugtiktok ─────────────────────────────────────────────────────────────
@tree.command(name="debugtiktok", description="Debug TikTok API response (admin only)")
@app_commands.checks.has_permissions(administrator=True)
async def debugtiktok(interaction: discord.Interaction, username: str):
    await interaction.response.defer(ephemeral=True)
    async with aiohttp.ClientSession() as session:
        try:
            profile = await _keyapi_get(session, "tiktok/influencer/detail", {"username": username})
            videos  = await _keyapi_get(session, "tiktok/influencer/videos", {"username": username, "count": 3})
        except ValueError as e:
            await interaction.followup.send(f"API error: {e}", ephemeral=True)
            return

    profile_str = json.dumps(profile, indent=2)[:1800]
    videos_str  = json.dumps(videos,  indent=2)[:1800]

    await interaction.followup.send(f"**Profile:**\n```json\n{profile_str}\n```", ephemeral=True)
    await interaction.followup.send(f"**Videos:**\n```json\n{videos_str}\n```", ephemeral=True)

# ── Startup ───────────────────────────────────────────────────────────────────
@bot.event
async def on_ready():
    bot.add_view(TicketPanelView())
    bot.add_view(TicketOpenView())
    bot.add_view(TicketClosedView())
    await tree.sync()
    weekly_reset.start()
    streak_checker.start()
    video_checker.start()
    await _load_streaks()
    _streaks_ready.set()
    await _load_ticket_counter()
    await post_ticket_panel()
    print(f"Logged in as {bot.user} ({bot.user.id})")

bot.run(TOKEN)
