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
import time as _time
from typing import Literal

# ── Config ───────────────────────────────────────────────────────────────────
TOKEN              = os.environ["DISCORD_BOT_TOKEN"]
YOUTUBE_API_KEY    = "AIzaSyAe5hyEAwxTCdBbZRQQsGfuQC6xlQWUBg0"
SCRAPECREATORS_API_KEY = "6JKSQFXTLsPDaL9MzqWwacyFhvt1"
SCRAPECREATORS_BASE    = "https://api.scrapecreators.com"
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
YOUTUBE_HANDLE           = "dreamyvr-official"
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
ADMIN_ROLE_ID       = 1423121100421861438
CC_ROLE_ID          = 1495165348654219344
MOD_PTS_DB_CHANNEL  = 1516176492663537875
DYNO_BOT_ID         = 155149108183695360
DYNO_PREFIXES       = ("?", "!", ".")
DYNO_MOD_LOG_CHANNEL_ID = 1423121107057246238  # Channel where Dyno posts its ban/kick logs
EVENTS_ROLE_ID      = 1423121100228923483      # "Events" role — pinging it earns mod points
PTS_BAN             = 2
PTS_WARN            = 2
PTS_KICK            = 2
PTS_THANKS          = 1
PTS_EVENTS_PING     = 2
PTS_PER_MSG_MILESTONE = 1
MSG_MILESTONE       = 75

# Only these specific moderators are shown on the leaderboard / counted toward
# the monthly requirement check, regardless of who else holds the mod role.
TRACKED_MOD_IDS = [
    939901393291051080,
    1342234964841861192,
    1120910285750943895,
    1354802771500597390,
    1382815397006540904,
    1016825802207268956,
    1253955909835554819,
    1277506981858639949,
    1294510847175032904,
    1089206891982372934,
    880643477292089414,
]

# ── Monthly moderator status report config ────────────────────────────────────
MOD_STATUS_CHANNEL_ID      = 1495388852020445255
MOD_STATUS_REQUIRED_POINTS = 10
MOD_STATUS_TIME            = time(0, 5)  # daily check time (UTC); only acts on the 1st
MOD_STATUS_MAG_EMOJI    = "<:Moderator_Magnifying_Glass:1497848294364418209>"
MOD_STATUS_CHECK_EMOJI  = "<:check:1498653592372903986>"
MOD_STATUS_CANCEL_EMOJI = "<:cancel:1508218455973957683>"

_staff_msg_counts: dict[str, int] = {}
THANKS_PATTERNS = re.compile(r"\b(thanks?|thank\s*you|ty|thx|tysm)\b", re.IGNORECASE)

# ── Cache ────────────────────────────────────────────────────────────────────
_approved_links_cache: dict | None = None
_approved_links_cache_time: float  = 0
CACHE_TTL = 300

# ── Counts cache ─────────────────────────────────────────────────────────────
_counts_cache: dict | None = None
_counts_cache_time: float  = 0
COUNTS_CACHE_TTL = 90  # seconds, matches counts_refresher interval

# ── DB write queue (batches message log writes to avoid rate limits) ──────────
_db_write_queue: list[str] = []

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
LEVEL_FIVE_TAG_ID      = 1520851613873602661
LEVEL_SIX_TAG_ID       = 1520853956086206725

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
    5: {
        "title": "Level 5",
        "content": (
            "A narrow hallway stretches endlessly into the darkness — quiet, empty, and "
            "suffocatingly still. Flickering fluorescent lights hum overhead as cold concrete "
            "walls close in on either side. The silence never lasts for long. Somewhere in the "
            "distance, heavy footsteps begin to echo, growing louder with every second. There "
            "is no fighting back. Only hiding... or running.\n\n"
            "**What to do:**\n"
            "* 🏃  Make your way to the opposite end of the hallway\n"
            "* 👁️  Watch and listen for the monster as it charges through the corridor\n"
            "* 🛡️  Hide in the safe spots along the walls whenever danger approaches\n"
            "* ⏳  Wait until the hallway is clear before continuing your journey\n"
            "* 🚪  Reach the exit at the far end to survive and escape"
        ),
        "images": ["level5_1.png", "level5_2.png"],
        "tag_id": LEVEL_FIVE_TAG_ID,
    },
    6: {
        "title": "Level 6",
        "content": (
            "A quiet neighborhood stretches out beneath a sky of heavy gray clouds. Identical "
            "houses line empty streets, disappearing into the thick fog. There are no signs of "
            "life—only an eerie silence that makes every step feel watched. Somewhere inside one "
            "of these homes lies the key you need, but it won't open the house it was found in. "
            "Your escape waits elsewhere.\n\n"
            "**What to do:**\n"
            "* 🔑 Search the houses until you find the hidden key\n"
            "* 🏠 Look for the house marked with a label—the key belongs there\n"
            "* 🚪 Unlock the labeled house to continue your journey\n"
            "* 👁️ Explore carefully, as not every house holds what you're looking for\n"
            "* 🚶 Find the correct door and move forward to escape the neighborhood"
        ),
        "images": ["level6_1.png", "level6_2.png"],
        "tag_id": LEVEL_SIX_TAG_ID,
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

def _is_admin(member) -> bool:
    if not member or not hasattr(member, "roles"):
        return False
    return any(r.id == ADMIN_ROLE_ID for r in member.roles)

def _is_cc(member) -> bool:
    if not member or not hasattr(member, "roles"):
        return False
    return any(r.id == CC_ROLE_ID for r in member.roles)

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

# ── Monthly moderator status report ───────────────────────────────────────────
async def _post_mod_status_report():
    """Builds and posts the Monthly Moderator Status embed-style message to
    MOD_STATUS_CHANNEL_ID. Only the fixed TRACKED_MOD_IDS roster is evaluated,
    each user needing >= MOD_STATUS_REQUIRED_POINTS to have "met" the
    requirement for the month."""
    channel = bot.get_channel(MOD_STATUS_CHANNEL_ID)
    if not channel:
        return
    guild = channel.guild
    tally = await _tally_mod_points()

    met, failed = [], []
    for uid in TRACKED_MOD_IDS:
        data   = tally.get(str(uid), {"total": 0, "breakdown": {}})
        points = data["total"]
        member = guild.get_member(uid) if guild else None
        name   = member.mention if member else f"<@{uid}>"
        (met if points >= MOD_STATUS_REQUIRED_POINTS else failed).append((name, points))

    met.sort(key=lambda e: e[1], reverse=True)
    failed.sort(key=lambda e: e[1], reverse=True)

    met_lines = "\n".join(
        f"> `{i + 1}.` {name} - `{points} mod points`" for i, (name, points) in enumerate(met)
    ) or "> None"
    failed_lines = "\n".join(
        f"> {name} - `{points} mod points`" for name, points in failed
    ) or "> None"

    content = (
        f"{MOD_STATUS_MAG_EMOJI}  __**Monthly Moderator Status**__ {MOD_STATUS_MAG_EMOJI} \n\n"
        f"{MOD_STATUS_CHECK_EMOJI}  **{len(met)} Moderators, have acquired the requirement** {MOD_STATUS_CHECK_EMOJI} \n"
        f"{MOD_STATUS_CANCEL_EMOJI}  **{len(failed)} Moderators, have failed the requirement** {MOD_STATUS_CANCEL_EMOJI}\n\n"
        "*Met Requirements:*\n"
        f"{met_lines}\n\n"
        "*Failed Requirements*\n"
        f"{failed_lines}"
    )
    await channel.send(content, allowed_mentions=discord.AllowedMentions.none())

async def _reset_mod_points_db():
    db = bot.get_channel(MOD_PTS_DB_CHANNEL)
    if db:
        await db.purge(limit=None)

@tasks.loop(time=MOD_STATUS_TIME)
async def monthly_mod_status():
    now = datetime.now(TIMEZONE)
    if now.day != 1:
        return
    await _post_mod_status_report()
    await _reset_mod_points_db()
    print(f"[{now}] Monthly moderator status posted and points database reset.")

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

def _extract_dyno_text_blobs(message: discord.Message) -> list[str]:
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
    return text_blobs

def _extract_dyno_invoker_id(message: discord.Message) -> int | None:
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
                return int(m.group(1))
    return None

async def handle_dyno_warn(message: discord.Message):
    if message.author.id != DYNO_BOT_ID or not message.guild:
        return
    combined = " ".join(_extract_dyno_text_blobs(message)).lower()
    if "warn" not in combined:
        return

    invoker_id = _extract_dyno_invoker_id(message)

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

async def handle_dyno_ban(message: discord.Message):
    """Detects bans carried out via Dyno by watching its mod log channel,
    since Dyno-issued bans show up in the audit log as executed by Dyno
    itself rather than by the moderator who ran the command."""
    if message.author.id != DYNO_BOT_ID or not message.guild:
        return
    if message.channel.id != DYNO_MOD_LOG_CHANNEL_ID:
        return

    combined = " ".join(_extract_dyno_text_blobs(message)).lower()
    if "ban" not in combined or "unban" in combined:
        return

    invoker_id = _extract_dyno_invoker_id(message)

    if not invoker_id:
        invoker = await _find_dyno_invoker(message.channel, "ban")
        if invoker:
            invoker_id = invoker.id

    if not invoker_id:
        return
    member = message.guild.get_member(invoker_id)
    if not _is_mod(member):
        return
    await _award_points(invoker_id, PTS_BAN, "ban")

async def handle_dyno_kick(message: discord.Message):
    """Detects kicks carried out via Dyno, the same way handle_dyno_ban does
    for bans — Dyno's kick log shows Dyno itself as the actor, not the mod."""
    if message.author.id != DYNO_BOT_ID or not message.guild:
        return
    if message.channel.id != DYNO_MOD_LOG_CHANNEL_ID:
        return

    combined = " ".join(_extract_dyno_text_blobs(message)).lower()
    if "kick" not in combined:
        return

    invoker_id = _extract_dyno_invoker_id(message)

    if not invoker_id:
        invoker = await _find_dyno_invoker(message.channel, "kick")
        if invoker:
            invoker_id = invoker.id

    if not invoker_id:
        return
    member = message.guild.get_member(invoker_id)
    if not _is_mod(member):
        return
    await _award_points(invoker_id, PTS_KICK, "kick")

# ── Track messages to prevent double points for "thanks" ─────────────────────
_thanked_messages: set[int] = set()
_thank_cooldowns: dict[str, str] = {}
_events_ping_cooldowns: dict[str, str] = {}

async def handle_mod_rewards(message: discord.Message):
    if message.author.bot or not message.guild:
        return
    author_member = message.guild.get_member(message.author.id)

    if _is_mod(author_member):
        uid = str(message.author.id)
        _staff_msg_counts[uid] = _staff_msg_counts.get(uid, 0) + 1
        if _staff_msg_counts[uid] >= MSG_MILESTONE:
            _staff_msg_counts[uid] = 0
            await _award_points(message.author.id, PTS_PER_MSG_MILESTONE, "messages")

        # Pinging the Events role earns points too, capped by a cooldown so it
        # can't be farmed by repeatedly pinging the same role for points.
        if any(r.id == EVENTS_ROLE_ID for r in message.role_mentions):
            now = datetime.now(pytz.UTC)
            uid = str(message.author.id)
            last_ping_str = _events_ping_cooldowns.get(uid)
            on_cooldown = False
            if last_ping_str:
                last_ping = datetime.fromisoformat(last_ping_str)
                on_cooldown = (now - last_ping) < timedelta(minutes=20)
            if not on_cooldown:
                _events_ping_cooldowns[uid] = now.isoformat()
                await _award_points(message.author.id, PTS_EVENTS_PING, "events_ping")

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

# ── ScrapeCreators helper ─────────────────────────────────────────────────────
async def _sc_get(session: aiohttp.ClientSession, path: str, params: dict = {}) -> dict:
    async with session.get(
        f"{SCRAPECREATORS_BASE}/{path.lstrip('/')}",
        headers={"x-api-key": SCRAPECREATORS_API_KEY},
        params=params,
    ) as r:
        raw = await r.text()
        if r.status != 200:
            raise ValueError(f"ScrapeCreators returned status {r.status}: {raw[:200]}")
        try:
            return json.loads(raw)
        except json.JSONDecodeError as e:
            raise ValueError(f"ScrapeCreators returned non-JSON: {raw[:200]}") from e

# ── Video announcement helpers ────────────────────────────────────────────────
_last_video_ids: dict[str, str] = {}
_last_youtube_channel_id: str | None = None
_video_ids_loaded = False

async def _load_last_video_ids():
    global _video_ids_loaded, _last_youtube_channel_id
    if _video_ids_loaded:
        return
    state_channel = bot.get_channel(VIDEO_STATE_CHANNEL_ID)
    if not state_channel:
        _video_ids_loaded = True
        return
    async for msg in state_channel.history(limit=None, oldest_first=True):
        if not msg.author.bot:
            continue
        if msg.content.startswith("LASTVIDEO:"):
            parts = msg.content.split(":", 2)
            if len(parts) != 3:
                continue
            _, platform, video_id = parts
            _last_video_ids[platform] = video_id
        elif msg.content.startswith("LASTYTCHANNEL:"):
            parts = msg.content.split(":", 1)
            if len(parts) == 2:
                _last_youtube_channel_id = parts[1]
    _video_ids_loaded = True

async def _save_last_video_id(platform: str, video_id: str):
    _last_video_ids[platform] = video_id
    state_channel = bot.get_channel(VIDEO_STATE_CHANNEL_ID)
    if state_channel:
        await state_channel.send(f"LASTVIDEO:{platform}:{video_id}")

async def _save_last_youtube_channel_id(channel_id: str):
    global _last_youtube_channel_id
    _last_youtube_channel_id = channel_id
    state_channel = bot.get_channel(VIDEO_STATE_CHANNEL_ID)
    if state_channel:
        await state_channel.send(f"LASTYTCHANNEL:{channel_id}")

async def get_latest_tiktok_video(username: str):
    """Returns (video_id, video_url) for the most recent post, or (None, None)."""
    async with aiohttp.ClientSession() as session:
        try:
            data = await _sc_get(session, "v3/tiktok/profile/videos", {"handle": username})
        except ValueError:
            return None, None
        videos = data.get("videos") or []
        if not videos:
            return None, None
        video    = videos[0]
        video_id = str(video.get("id") or video.get("video_id") or "")
        if not video_id:
            return None, None
        return video_id, f"https://www.tiktok.com/@{username}/video/{video_id}"

async def get_latest_youtube_video(handle: str):
    """Returns (video_id, video_url, channel_id) for the most recent upload, or (None, None, None)."""
    base = "https://www.googleapis.com/youtube/v3"
    async with aiohttp.ClientSession() as session:
        params = {"part": "id", "forHandle": f"@{handle}", "key": YOUTUBE_API_KEY}
        async with session.get(f"{base}/channels", params=params) as r:
            data = await r.json()
        items = data.get("items", [])
        if not items:
            return None, None, None
        channel_id = items[0]["id"]

        search_params = {
            "part": "id", "channelId": channel_id, "type": "video",
            "order": "date", "maxResults": 1, "key": YOUTUBE_API_KEY,
        }
        async with session.get(f"{base}/search", params=search_params) as r:
            sdata = await r.json()
        sitems = sdata.get("items", [])
        if not sitems:
            return None, None, channel_id
        video_id = sitems[0].get("id", {}).get("videoId")
        if not video_id:
            return None, None, channel_id
        return video_id, f"https://www.youtube.com/watch?v={video_id}", channel_id

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
        yt_id, yt_url, yt_channel_id = await get_latest_youtube_video(YOUTUBE_HANDLE)
        if yt_id and yt_channel_id:
            # If the resolved channel differs from the last one we tracked
            # (e.g. the handle/channel changed), treat this as a fresh
            # baseline rather than announcing whatever's currently latest.
            channel_changed = (
                _last_youtube_channel_id is not None
                and yt_channel_id != _last_youtube_channel_id
            )
            is_first_check = "youtube" not in _last_video_ids or channel_changed

            if yt_channel_id != _last_youtube_channel_id:
                await _save_last_youtube_channel_id(yt_channel_id)

            if yt_id != _last_video_ids.get("youtube") or channel_changed:
                await _save_last_video_id("youtube", yt_id)
                if not is_first_check:
                    await _announce_video(yt_url)
    except Exception as e:
        print(f"[video_checker] YouTube check failed: {e}")

# ── Log every message into the DB channel ────────────────────────────────────
@bot.event
async def on_message(message: discord.Message):
    await handle_dyno_warn(message)
    await handle_dyno_ban(message)
    await handle_dyno_kick(message)

    if message.author.bot:
        return
    if message.channel.id in (DB_CHANNEL_ID, 1500327292830875898, STREAK_DB_CHANNEL, MOD_PTS_DB_CHANNEL):
        return

    db_channel = bot.get_channel(DB_CHANNEL_ID)
    if db_channel:
        _db_write_queue.append(str(message.author.id))

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

# ── Ban detection (fallback for native Discord bans, e.g. right-click → Ban) ─
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

# ── Helper: tally message counts (with 5-minute cache) ───────────────────────
async def tally_counts() -> dict:
    global _counts_cache, _counts_cache_time
    now = _time.monotonic()
    if _counts_cache is not None and (now - _counts_cache_time) < COUNTS_CACHE_TTL:
        return _counts_cache
    db_channel = bot.get_channel(DB_CHANNEL_ID)
    counts = {}
    async for msg in db_channel.history(limit=None, oldest_first=True):
        if msg.author.bot:
            for uid in msg.content.strip().splitlines():
                uid = uid.strip()
                if uid.isdigit():
                    counts[uid] = counts.get(uid, 0) + 1
    _counts_cache = counts
    _counts_cache_time = now
    return counts

# ── Background task: flush queued DB writes every 5 seconds ──────────────────
@tasks.loop(seconds=5)
async def db_flusher():
    if not _db_write_queue:
        return
    db_channel = bot.get_channel(DB_CHANNEL_ID)
    if not db_channel:
        return
    batch = _db_write_queue.copy()
    _db_write_queue.clear()
    content = "\n".join(batch)
    for i in range(0, len(content), 1900):
        await db_channel.send(content[i:i+1900])

# ── Background task: refresh counts cache every 90 seconds ───────────────────
@tasks.loop(seconds=90)
async def counts_refresher():
    global _counts_cache, _counts_cache_time
    db_channel = bot.get_channel(DB_CHANNEL_ID)
    if not db_channel:
        return
    counts = {}
    async for msg in db_channel.history(limit=None, oldest_first=True):
        if msg.author.bot:
            for uid in msg.content.strip().splitlines():
                uid = uid.strip()
                if uid.isdigit():
                    counts[uid] = counts.get(uid, 0) + 1
    _counts_cache = counts
    _counts_cache_time = _time.monotonic()
    now_str = datetime.now(pytz.UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    await db_channel.send(f"SCAN:{now_str}:{len(counts)}")
    print(f"[counts_refresher] Cache refreshed — {len(counts)} users tracked.")

# ── Helper: build approved links cache ───────────────────────────────────────
async def _build_links_cache() -> dict:
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

        # Fetch channel stats + the "uploads" playlist ID in one call. We use the
        # uploads playlist (not search.list) to find #dreamyvr videos, because
        # YouTube's search.list endpoint uses a separate, often-incomplete search
        # index that unreliably misses videos — especially on smaller channels —
        # even when the hashtag is clearly present. Scanning the uploads playlist
        # ourselves is both reliable and far cheaper on API quota.
        params = {"part": "snippet,statistics,contentDetails", "id": channel_id, "key": YOUTUBE_API_KEY}
        async with session.get(f"{base}/channels", params=params) as r:
            data = await r.json()
        items = data.get("items", [])
        if not items:
            raise ValueError("Could not fetch YouTube channel stats.")
        ch      = items[0]
        stats   = ch.get("statistics", {})
        snippet = ch.get("snippet", {})
        uploads_playlist_id = (
            ch.get("contentDetails", {}).get("relatedPlaylists", {}).get("uploads")
        )

        dreamyvr_views = 0
        dreamyvr_count = 0

        if uploads_playlist_id:
            hashtag_video_ids = []
            next_page_token = None
            while True:
                pl_params = {
                    "part": "snippet", "playlistId": uploads_playlist_id,
                    "maxResults": 50, "key": YOUTUBE_API_KEY,
                }
                if next_page_token:
                    pl_params["pageToken"] = next_page_token
                async with session.get(f"{base}/playlistItems", params=pl_params) as pr:
                    pdata = await pr.json()
                for item in pdata.get("items", []):
                    sn   = item.get("snippet", {})
                    text = f"{sn.get('title', '')} {sn.get('description', '')}".lower()
                    if "#dreamyvr" in text:
                        vid = sn.get("resourceId", {}).get("videoId")
                        if vid:
                            hashtag_video_ids.append(vid)
                next_page_token = pdata.get("nextPageToken")
                if not next_page_token:
                    break

            # Batch-fetch view counts for the matched videos, 50 at a time
            for i in range(0, len(hashtag_video_ids), 50):
                chunk = hashtag_video_ids[i:i + 50]
                stats_params = {"part": "statistics", "id": ",".join(chunk), "key": YOUTUBE_API_KEY}
                async with session.get(f"{base}/videos", params=stats_params) as vr:
                    vdata = await vr.json()
                for v in vdata.get("items", []):
                    dreamyvr_count += 1
                    dreamyvr_views += int(v.get("statistics", {}).get("viewCount", 0))

        return {
            "channel_name":   snippet.get("title", "Unknown"),
            "subscribers":    int(stats.get("subscriberCount", 0)),
            "total_views":    int(stats.get("viewCount", 0)),
            "total_videos":   int(stats.get("videoCount", 0)),
            "dreamyvr_count": dreamyvr_count,
            "dreamyvr_views": dreamyvr_views,
            "channel_url":    f"https://www.youtube.com/channel/{channel_id}",
        }

# ── TikTok helpers (ScrapeCreators) ──────────────────────────────────────────
def extract_tiktok_username(url: str):
    m = re.search(r"tiktok\.com/@([A-Za-z0-9_\.]+)", url)
    return m.group(1) if m else None

async def fetch_tiktok_posts_data(username: str):
    """Returns (total_dreamyvr_views, dreamyvr_video_count, follower_count).

    Uses the hashtag search endpoint to find all #dreamyvr videos, then filters
    by username so we only count videos from this specific creator.
    """
    total_views    = 0
    video_count    = 0
    follower_count = 0
    seen_ids: set[str] = set()

    async with aiohttp.ClientSession() as session:
        # Fetch follower count from profile endpoint
        try:
            profile_data   = await _sc_get(session, "v1/tiktok/profile", {"handle": username})
            follower_count = int(
                profile_data.get("stats", {}).get("followerCount", 0)
                or profile_data.get("followerCount", 0)
                or 0
            )
        except ValueError:
            pass

        # Search #dreamyvr hashtag and filter results by this creator's username
        cursor   = None
        has_more = True
        while has_more:
            params = {"hashtag": "dreamyvr"}
            if cursor:
                params["cursor"] = cursor
            try:
                data = await _sc_get(session, "v1/tiktok/search/hashtag", params)
            except ValueError:
                break

            videos   = data.get("videos") or data.get("aweme_list") or []
            has_more = bool(data.get("hasMore", data.get("has_more", False)))
            cursor   = data.get("cursor") if has_more else None

            if not videos:
                break

            for video in videos:
                # Get the author's unique_id to match against our username
                author = video.get("author") or {}
                vid_username = (
                    author.get("uniqueId")
                    or author.get("unique_id")
                    or video.get("authorUniqueId")
                    or video.get("author_unique_id")
                    or ""
                ).lower()

                if vid_username != username.lower():
                    continue

                # Deduplicate (TikTok hashtag search can return duplicates)
                vid_id = str(
                    video.get("id")
                    or video.get("aweme_id")
                    or video.get("video_id")
                    or ""
                )
                if vid_id and vid_id in seen_ids:
                    continue
                if vid_id:
                    seen_ids.add(vid_id)

                stats      = video.get("stats") or video.get("statistics") or {}
                play_count = (
                    stats.get("playCount")
                    or stats.get("play_count")
                    or video.get("playCount")
                    or video.get("play_count")
                    or 0
                )
                total_views += int(play_count)
                video_count += 1

            if not has_more or not cursor:
                break

    return total_views, video_count, follower_count

async def fetch_tiktok_totals(username: str):
    """Best-effort: returns (total_views, total_videos) by paging through this
    creator's own video list, or (None, None) if that endpoint doesn't return
    usable data. Kept separate from the (proven-working) hashtag search above
    so that if this fails, it doesn't take the whole /ccstats command down
    with it — the UI just shows "Unavailable" for these two fields."""
    total_views  = 0
    total_videos = 0
    got_any      = False

    async with aiohttp.ClientSession() as session:
        cursor = None
        while True:
            params = {"handle": username}
            if cursor:
                params["cursor"] = cursor
            try:
                data = await _sc_get(session, "v3/tiktok/profile/videos", params)
            except ValueError:
                break

            videos = (
                data.get("videos")
                or data.get("aweme_list")
                or data.get("itemList")
                or data.get("item_list")
                or []
            )
            if not videos:
                break

            for video in videos:
                stats = video.get("stats") or video.get("statistics") or {}
                play_count = (
                    stats.get("playCount")
                    or stats.get("play_count")
                    or video.get("playCount")
                    or video.get("play_count")
                )
                if play_count is None:
                    continue
                total_views  += int(play_count)
                total_videos += 1
                got_any = True

            has_more = bool(data.get("hasMore", data.get("has_more", False)))
            cursor   = data.get("cursor") if has_more else None
            if not has_more or not cursor:
                break

    if not got_any:
        return None, None
    return total_views, total_videos

def _fmt_stat(value) -> str:
    """Formats a stat for display, showing 'Unavailable' if it's None
    (e.g. TikTok totals when that endpoint didn't return usable data)."""
    return f"{value:,}" if value is not None else "Unavailable"

async def fetch_tiktok_stats(url: str):
    username = extract_tiktok_username(url)
    if not username:
        raise ValueError("Couldn't parse that TikTok URL.")
    dreamyvr_views, dreamyvr_count, followers = await fetch_tiktok_posts_data(username)
    total_views, total_videos = await fetch_tiktok_totals(username)
    async with aiohttp.ClientSession() as session:
        try:
            profile_data = await _sc_get(session, "v1/tiktok/profile", {"handle": username})
            # Try common nickname field locations
            nickname = (
                profile_data.get("user", {}).get("nickname")
                or profile_data.get("nickname")
                or username
            )
        except Exception:
            nickname = username
    return {
        "channel_name":   nickname,
        "username":       username,
        "channel_url":    f"https://www.tiktok.com/@{username}",
        "followers":      followers,
        "total_views":    total_views,
        "total_videos":   total_videos,
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
    member = interaction.guild.get_member(interaction.user.id) if interaction.guild else None
    if not _is_cc(member):
        await interaction.response.send_message("You don't have permission to use this command.", ephemeral=True)
        return
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
    member = interaction.guild.get_member(interaction.user.id) if interaction.guild else None
    if not _is_cc(member):
        await interaction.response.send_message("You don't have permission to use this command.", ephemeral=True)
        return
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
            embed.add_field(name="Total Views",       value=f"`{stats['total_views']:,}`",      inline=True)
            embed.add_field(name="Total Videos",      value=f"`{stats['total_videos']:,}`",     inline=True)
            embed.add_field(name="#dreamyvr Videos",  value=f"`{stats['dreamyvr_count']:,}`",   inline=True)
            embed.add_field(name="#dreamyvr Views",   value=f"`{stats['dreamyvr_views']:,}`",   inline=True)
            embed.set_footer(text=f"Requested by {interaction.user}")
            await interaction.edit_original_response(content=None, embed=embed)
        elif platform == "TikTok":
            stats = await fetch_tiktok_stats(url)
            embed = discord.Embed(title=stats["channel_name"], url=stats["channel_url"], color=0x010101)
            embed.add_field(name="Platform",         value="TikTok",                         inline=True)
            embed.add_field(name="Followers",        value=f"`{stats['followers']:,}`",      inline=True)
            embed.add_field(name="Total Views",      value=f"`{_fmt_stat(stats['total_views'])}`",    inline=True)
            embed.add_field(name="Total Videos",     value=f"`{_fmt_stat(stats['total_videos'])}`",   inline=True)
            embed.add_field(name="#dreamyvr Videos", value=f"`{stats['dreamyvr_count']:,}`", inline=True)
            embed.add_field(name="#dreamyvr Views",  value=f"`{stats['dreamyvr_views']:,}`", inline=True)
            embed.set_footer(text=f"Requested by {interaction.user}")
            await interaction.edit_original_response(content=None, embed=embed)
    except ValueError as e:
        await interaction.edit_original_response(content=f"Error fetching stats: {e}")
    except Exception as e:
        await interaction.edit_original_response(content=f"Something went wrong: {e}")

# ── /message ─────────────────────────────────────────────────────────────────
MESSAGE_ALLOWED_ROLE_ID = 1520597775581052928

@tree.command(name="message", description="Send a message as the bot")
@app_commands.describe(content="The message to send")
async def message(interaction: discord.Interaction, content: str):
    member = interaction.guild.get_member(interaction.user.id) if interaction.guild else None
    if not member or not any(r.id == MESSAGE_ALLOWED_ROLE_ID for r in member.roles):
        await interaction.response.send_message("You don't have permission to use this command.", ephemeral=True)
        return
    await interaction.channel.send(content)
    await interaction.response.send_message("Message sent!", ephemeral=True)

# ── /adminlink ───────────────────────────────────────────────────────────────
@tree.command(name="adminlink", description="Manually approve a link for a user")
@app_commands.describe(user="The Discord user", platform="Platform", handle="YouTube or TikTok URL/handle")
async def adminlink(interaction: discord.Interaction, user: discord.Member, platform: Literal["YouTube", "TikTok"], handle: str):
    member = interaction.guild.get_member(interaction.user.id) if interaction.guild else None
    if not _is_admin(member):
        await interaction.response.send_message("You don't have permission to use this command.", ephemeral=True)
        return
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
async def adminccstats(interaction: discord.Interaction, user: discord.Member, platform: Literal["YouTube", "TikTok"]):
    member = interaction.guild.get_member(interaction.user.id) if interaction.guild else None
    if not _is_admin(member):
        await interaction.response.send_message("You don't have permission to use this command.", ephemeral=True)
        return
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
            embed.add_field(name="Total Views",      value=f"`{stats['total_views']:,}`",    inline=True)
            embed.add_field(name="Total Videos",     value=f"`{stats['total_videos']:,}`",   inline=True)
            embed.add_field(name="#dreamyvr Videos", value=f"`{stats['dreamyvr_count']:,}`", inline=True)
            embed.add_field(name="#dreamyvr Views",  value=f"`{stats['dreamyvr_views']:,}`", inline=True)
            embed.set_footer(text=f"Requested by {interaction.user} • For {user}")
            await interaction.edit_original_response(content=None, embed=embed)
        elif platform == "TikTok":
            stats = await fetch_tiktok_stats(url)
            embed = discord.Embed(title=stats["channel_name"], url=stats["channel_url"], color=0x010101)
            embed.add_field(name="Platform",         value="TikTok",                         inline=True)
            embed.add_field(name="Followers",        value=f"`{stats['followers']:,}`",      inline=True)
            embed.add_field(name="Total Views",      value=f"`{_fmt_stat(stats['total_views'])}`",    inline=True)
            embed.add_field(name="Total Videos",     value=f"`{_fmt_stat(stats['total_videos'])}`",   inline=True)
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
    member = interaction.guild.get_member(interaction.user.id) if interaction.guild else None
    if not _is_admin(member):
        await interaction.response.send_message("You don't have permission to use this command.", ephemeral=True)
        return
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
                    f"> Total views: {_fmt_stat(stats['total_views'])}\n"
                    f"> Total videos: {_fmt_stat(stats['total_videos'])}\n"
                    f"> #dreamyvr videos: {stats['dreamyvr_count']:,}\n"
                    f"> #dreamyvr total views: {stats['dreamyvr_views']:,}"
                )
            elif platform == "YouTube":
                stats = await fetch_youtube_stats(url)
                lines.append(
                    f"**{number}.** {mention}\n"
                    f"> URL: {url}\n"
                    f"> Subscribers: {stats['subscribers']:,}\n"
                    f"> Total views: {stats['total_views']:,}\n"
                    f"> Total videos: {stats['total_videos']:,}\n"
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
    embed.add_field(name=f"Messages (×1/{MSG_MILESTONE})", value=f"`{bd.get('messages', 0)}`", inline=True)
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
    ranked = []
    for uid in TRACKED_MOD_IDS:
        data = tally.get(str(uid), {"total": 0, "breakdown": {}})
        ranked.append((str(uid), data))
    ranked.sort(key=lambda kv: kv[1]["total"], reverse=True)
    lines = []
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

# ── /moderatorstatus ─────────────────────────────────────────────────────────
@tree.command(name="moderatorstatus", description="Post the monthly moderator status report (admin only)")
async def moderatorstatus(interaction: discord.Interaction):
    member = interaction.guild.get_member(interaction.user.id) if interaction.guild else None
    if not _is_admin(member):
        await interaction.response.send_message("You don't have permission to use this command.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    await _post_mod_status_report()
    await interaction.followup.send(f"Posted the moderator status report in <#{MOD_STATUS_CHANNEL_ID}>.", ephemeral=True)

# ── /sendlevel ────────────────────────────────────────────────────────────────
def _has_level_role(member: discord.Member) -> bool:
    if not member or not hasattr(member, "roles"):
        return False
    return any(r.id == LEVEL_ROLE_ID for r in member.roles)

@tree.command(name="sendlevel", description="Post a level guide to the forum (restricted)")
@app_commands.describe(level="Which level to post")
async def sendlevel(interaction: discord.Interaction, level: Literal[1, 2, 3, 4, 5, 6]):
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

    # Invalidate counts cache after purge so stale data isn't served
    global _counts_cache, _counts_cache_time
    _counts_cache      = None
    _counts_cache_time = 0

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
async def debugtiktok(interaction: discord.Interaction, username: str):
    member = interaction.guild.get_member(interaction.user.id) if interaction.guild else None
    if not _is_admin(member):
        await interaction.response.send_message("You don't have permission to use this command.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    async with aiohttp.ClientSession() as session:
        try:
            profile = await _sc_get(session, "v1/tiktok/profile", {"handle": username})
            videos  = await _sc_get(session, "v3/tiktok/profile/videos", {"handle": username})
        except ValueError as e:
            await interaction.followup.send(f"API error: {e}", ephemeral=True)
            return

    profile_str = json.dumps(profile, indent=2)[:1800]
    videos_str  = json.dumps(videos,  indent=2)[:1800]

    await interaction.followup.send(f"**Profile:**\n```json\n{profile_str}\n```", ephemeral=True)
    await interaction.followup.send(f"**Videos:**\n```json\n{videos_str}\n```", ephemeral=True)

# ── /debugdyno ───────────────────────────────────────────────────────────────
@tree.command(name="debugdyno", description="Dump the raw content/embeds of recent Dyno mod-log messages (admin only)")
async def debugdyno(interaction: discord.Interaction, limit: int = 5):
    member = interaction.guild.get_member(interaction.user.id) if interaction.guild else None
    if not _is_admin(member):
        await interaction.response.send_message("You don't have permission to use this command.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    channel = bot.get_channel(DYNO_MOD_LOG_CHANNEL_ID)
    if not channel:
        await interaction.followup.send("Could not reach the Dyno mod log channel.", ephemeral=True)
        return
    out_lines = []
    async for msg in channel.history(limit=limit):
        if msg.author.id != DYNO_BOT_ID:
            continue
        out_lines.append(f"--- message {msg.id} ---")
        out_lines.append(f"content: {msg.content!r}")
        for e in msg.embeds:
            out_lines.append(f"embed.title: {e.title!r}")
            out_lines.append(f"embed.description: {e.description!r}")
            for f in e.fields:
                out_lines.append(f"  field: {f.name!r} = {f.value!r}")
            if e.footer:
                out_lines.append(f"embed.footer: {e.footer.text!r}")
            if e.author:
                out_lines.append(f"embed.author: {e.author.name!r}")
    if not out_lines:
        await interaction.followup.send("No Dyno messages found in that channel.", ephemeral=True)
        return
    text = "\n".join(out_lines)[:1900]
    await interaction.followup.send(f"```\n{text}\n```", ephemeral=True)

# ═════════════════════════════════════════════════════════════════════════════
# ── Moderation commands (/ban, /kick, /warn, /warnings) ──────────────────────
# ═════════════════════════════════════════════════════════════════════════════

MOD_ACTION_ROLE_ID  = ADMIN_ROLE_ID          # only admins can use /ban /kick /warn /warnings
WARN_DB_CHANNEL_ID  = 1516176492663537875    # channel used to persist warning records (shared with mod points — safe, distinguished by message prefix)

async def _save_warning(guild_id: int, target: discord.Member, moderator: discord.Member, reason: str) -> dict:
    """Appends a warning record to the DB channel and returns the record."""
    record = {
        "guild_id":   guild_id,
        "user_id":    target.id,
        "user_name":  str(target),
        "mod_id":     moderator.id,
        "mod_name":   moderator.display_name,
        "reason":     reason,
        "timestamp":  datetime.now(pytz.UTC).isoformat(),
    }
    db = bot.get_channel(WARN_DB_CHANNEL_ID)
    if db:
        await db.send(f"WARNING:{json.dumps(record)}")
    return record

async def _get_warnings(guild_id: int, user_id: int) -> list[dict]:
    """Returns all active warnings for a user in a guild."""
    db = bot.get_channel(WARN_DB_CHANNEL_ID)
    if not db:
        return []
    warnings_list = []
    async for msg in db.history(limit=None, oldest_first=True):
        if not msg.author.bot:
            continue
        content = msg.content
        if content.startswith("WARNING:"):
            try:
                record = json.loads(content[len("WARNING:"):])
                if record.get("guild_id") == guild_id and record.get("user_id") == user_id:
                    record["_msg_id"] = msg.id
                    warnings_list.append(record)
            except Exception:
                continue
        elif content.startswith("DELWARN:"):
            try:
                del_info = json.loads(content[len("DELWARN:"):])
                # Remove the warning whose message id matches
                warnings_list = [w for w in warnings_list if w.get("_msg_id") != del_info.get("msg_id")]
            except Exception:
                continue
    return warnings_list

async def _delete_warning_by_index(guild_id: int, user_id: int, index: int) -> bool:
    """Marks warning at 0-based index as deleted. Returns True on success."""
    warnings_list = await _get_warnings(guild_id, user_id)
    if index < 0 or index >= len(warnings_list):
        return False
    target_msg_id = warnings_list[index].get("_msg_id")
    if not target_msg_id:
        return False
    db = bot.get_channel(WARN_DB_CHANNEL_ID)
    if db:
        await db.send(f"DELWARN:{json.dumps({'guild_id': guild_id, 'user_id': user_id, 'msg_id': target_msg_id})}")
    return True

def _time_ago(iso_str: str) -> str:
    """Returns a human-readable 'X ago' string from an ISO timestamp."""
    try:
        dt  = datetime.fromisoformat(iso_str)
        now = datetime.now(pytz.UTC)
        diff = now - dt
        seconds = int(diff.total_seconds())
        if seconds < 60:
            return "just now"
        elif seconds < 3600:
            m = seconds // 60
            return f"{m} minute{'s' if m != 1 else ''} ago"
        elif seconds < 86400:
            h = seconds // 3600
            return f"{h} hour{'s' if h != 1 else ''} ago"
        elif seconds < 86400 * 30:
            d = seconds // 86400
            return f"{d} day{'s' if d != 1 else ''} ago"
        elif seconds < 86400 * 365:
            mo = seconds // (86400 * 30)
            return f"{mo} month{'s' if mo != 1 else ''} ago"
        else:
            y = seconds // (86400 * 365)
            return f"{y} year{'s' if y != 1 else ''} ago"
    except Exception:
        return "unknown"

def _can_moderate(member) -> bool:
    if not member or not hasattr(member, "roles"):
        return False
    return any(r.id == MOD_ACTION_ROLE_ID for r in member.roles)

# ── Delete Warning button view ────────────────────────────────────────────────
class DeleteWarningView(discord.ui.View):
    def __init__(self, target_id: int, guild_id: int):
        super().__init__(timeout=300)
        self.target_id = target_id
        self.guild_id  = guild_id

    @discord.ui.button(label="Delete a warning", style=discord.ButtonStyle.danger, emoji="🗑️")
    async def delete_warning(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not _can_moderate(interaction.user):
            await interaction.response.send_message("You don't have permission to delete warnings.", ephemeral=True)
            return

        warnings_list = await _get_warnings(self.guild_id, self.target_id)
        if not warnings_list:
            await interaction.response.send_message("This user has no warnings to delete.", ephemeral=True)
            return

        # Build a select with each warning as an option
        options = []
        for i, w in enumerate(warnings_list):
            label = f"#{i+1} — {w.get('reason', 'No reason')}"[:100]
            options.append(discord.SelectOption(label=label, value=str(i)))

        class WarningSelectView(discord.ui.View):
            def __init__(self_inner):
                super().__init__(timeout=60)

            @discord.ui.select(placeholder="Select a warning to delete...", options=options)
            async def select_cb(self_inner, select_interaction: discord.Interaction, select: discord.ui.Select):
                idx = int(select.values[0])
                success = await _delete_warning_by_index(self.guild_id, self.target_id, idx)
                if success:
                    await select_interaction.response.send_message(f"✅ Warning #{idx+1} deleted.", ephemeral=True)
                else:
                    await select_interaction.response.send_message("Failed to delete that warning.", ephemeral=True)

        await interaction.response.send_message("Select the warning to remove:", view=WarningSelectView(), ephemeral=True)

# ── /ban ──────────────────────────────────────────────────────────────────────
@tree.command(name="ban", description="Ban a member from the server")
@app_commands.describe(user="The member to ban", reason="Reason for the ban")
async def ban(interaction: discord.Interaction, user: discord.Member, reason: str):
    invoker = interaction.guild.get_member(interaction.user.id) if interaction.guild else None
    if not _can_moderate(invoker):
        await interaction.response.send_message("You don't have permission to use this command.", ephemeral=True)
        return

    await interaction.response.defer()

    try:
        # DM the user before banning
        try:
            await user.send(f"You have been **banned** from **{interaction.guild.name}**.\n**Reason:** {reason}")
        except discord.Forbidden:
            pass

        await user.ban(reason=f"{reason} (banned by {interaction.user})")

        embed = discord.Embed(
            description=f"✅ **{user}** was banned.",
            color=0x2ECC71,
        )
        await interaction.followup.send(embed=embed)

    except discord.Forbidden:
        await interaction.followup.send("I don't have permission to ban that user.", ephemeral=True)
    except discord.HTTPException as e:
        await interaction.followup.send(f"Ban failed: {e}", ephemeral=True)

# ── /kick ─────────────────────────────────────────────────────────────────────
@tree.command(name="kick", description="Kick a member from the server")
@app_commands.describe(user="The member to kick", reason="Reason for the kick")
async def kick(interaction: discord.Interaction, user: discord.Member, reason: str):
    invoker = interaction.guild.get_member(interaction.user.id) if interaction.guild else None
    if not _can_moderate(invoker):
        await interaction.response.send_message("You don't have permission to use this command.", ephemeral=True)
        return

    await interaction.response.defer()

    try:
        try:
            await user.send(f"You have been **kicked** from **{interaction.guild.name}**.\n**Reason:** {reason}")
        except discord.Forbidden:
            pass

        await user.kick(reason=f"{reason} (kicked by {interaction.user})")

        embed = discord.Embed(
            description=f"✅ **{user}** was kicked.",
            color=0x2ECC71,
        )
        await interaction.followup.send(embed=embed)

    except discord.Forbidden:
        await interaction.followup.send("I don't have permission to kick that user.", ephemeral=True)
    except discord.HTTPException as e:
        await interaction.followup.send(f"Kick failed: {e}", ephemeral=True)

# ── /warn ─────────────────────────────────────────────────────────────────────
@tree.command(name="warn", description="Warn a member")
@app_commands.describe(user="The member to warn", reason="Reason for the warning")
async def warn(interaction: discord.Interaction, user: discord.Member, reason: str):
    invoker = interaction.guild.get_member(interaction.user.id) if interaction.guild else None
    if not _can_moderate(invoker):
        await interaction.response.send_message("You don't have permission to use this command.", ephemeral=True)
        return

    await interaction.response.defer()

    await _save_warning(interaction.guild_id, user, interaction.user, reason)

    try:
        await user.send(f"You have been **warned** in **{interaction.guild.name}**.\n**Reason:** {reason}")
    except discord.Forbidden:
        pass

    embed = discord.Embed(
        description=f"✅ **{user}** has been warned. || {reason}",
        color=0x2ECC71,
    )
    await interaction.followup.send(embed=embed)

# ── /warnings ────────────────────────────────────────────────────────────────
@tree.command(name="warnings", description="View warnings for a member")
@app_commands.describe(user="The member to check")
async def warnings(interaction: discord.Interaction, user: discord.Member):
    invoker = interaction.guild.get_member(interaction.user.id) if interaction.guild else None
    if not _can_moderate(invoker):
        await interaction.response.send_message("You don't have permission to use this command.", ephemeral=True)
        return

    await interaction.response.defer()

    warns = await _get_warnings(interaction.guild_id, user.id)

    count = len(warns)
    embed = discord.Embed(
        title=f"{count} Warning{'s' if count != 1 else ''} for {user.display_name} ({user.id})",
        color=0x2b2d31,
    )
    embed.set_thumbnail(url=user.display_avatar.url)

    if not warns:
        embed.description = "This user has no warnings."
        await interaction.followup.send(embed=embed)
        return

    for w in warns:
        mod_name = w.get("mod_name", "Unknown")
        reason   = w.get("reason", "No reason provided")
        time_str = _time_ago(w.get("timestamp", ""))
        embed.add_field(
            name=f"Moderator: {mod_name}",
            value=f"{reason} — {time_str}",
            inline=False,
        )

    view = DeleteWarningView(target_id=user.id, guild_id=interaction.guild_id)
    await interaction.followup.send(embed=embed, view=view)

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
    counts_refresher.start()
    db_flusher.start()
    monthly_mod_status.start()
    asyncio.create_task(_delayed_counts_warmup())
    await _load_streaks()
    _streaks_ready.set()
    await _load_ticket_counter()
    await post_ticket_panel()
    print(f"Logged in as {bot.user} ({bot.user.id})")

async def _delayed_counts_warmup():
    await asyncio.sleep(60)
    await tally_counts()
    print("[counts] Cache warmed up.")

bot.run(TOKEN)
