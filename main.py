from __future__ import annotations

import os
import io
import re
import json
import logging
import asyncio
import time
from datetime import timedelta, datetime, timezone
from typing import Literal

import aiohttp
import emoji as emoji_lib
import discord
from discord import app_commands
from discord.ext import commands, tasks
from playwright.async_api import async_playwright
from PIL import Image, ImageDraw, ImageFont

# Autocomplete responses can occasionally arrive after Discord has already invalidated
# the interaction (e.g. the user typed another character before the bot replied).
# discord.py already handles this gracefully — it just logs a full traceback as noise.
# Filter that specific, harmless message out so real errors aren't buried under it.
class _SuppressAutocompleteRaceNoise(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return "Ignoring exception in autocomplete" not in record.getMessage()


logging.getLogger("discord.app_commands.tree").addFilter(_SuppressAutocompleteRaceNoise())

# ---------- Config ----------
CONFIRM_CHANNEL_ID = 1528146431138074624   # admins confirm new teams here
TEAM_CATEGORY_ID = 1528146975554404552     # category new team channels are created in
TEAM_CATEGORY_OVERFLOW_ID = 1534991567486714026  # used once TEAM_CATEGORY_ID hits Discord's 50-channel cap
TEAM_LOG_CHANNEL_ID = 1530008905663512626  # teams JSON "database" message lives here
TEAM_ACTIVITY_LOG_CHANNEL_ID = 1536846101657690242  # human-readable embed log of every team event (create/delete/join/leave/invite/etc)
BOT_LOG_CHANNEL_ID = 1538791773772455949   # global bot activity log — every slash command run, every JSON db sync (backup/restore), and every startup/restart
GIVEAWAY_LOG_CHANNEL_ID = 1530009058294370476  # giveaways JSON "database" message lives here
LOG_CHANNEL_ID = 1528147225799037008       # legacy combined database channel — kept only so
                                            # old data can be migrated into the two channels above
REFERENCE_ROLE_ID = 1528009686509420616    # team roles are kept positioned just above this role
STAFF_ROLE_ID = 1528009567219224616        # only holders of this role can use staff team-management commands
PREMIUM_ROLE_ID = 1528139462159106059      # gates /premiumteamsettings; premium team roles are kept above this role
PREMIUM_ROLE_ID_2 = 1529805001088569384    # a second role that also grants premium access
TEAM_LEADER_ROLE_ID = 1528445357317423135  # granted to every team leader, current and future
TEAM_CO_LEADER_ROLE_ID = 1543045614319968266  # granted to a team's co-leader (see /setcoleader), removed once they no longer hold that slot
MAX_TEAM_MEMBERS = 10                      # includes the leader
TEAM_JOIN_COOLDOWN_DAYS = 7                 # how long a member must stay on a team before leaving it
SUPPORT_TICKET_CHANNEL_ID = 1542648891147558993  # the support ticket panel is posted/refreshed here, and new ticket threads are opened here
TICKET_PING_ROLE_ID = 1528224254896771132        # pinged (alongside the opener) whenever a new ticket thread is opened
TICKET_LOG_CHANNEL_ID = 1533595017438826646       # ticket numbers/records JSON "database" message lives here
TICKET_CLOSE_ROLE_ID = 1528142703727083691        # holders of this role can close any ticket, same as staff
INVITE_TRACKER_CHANNEL_ID = 1528160701955313722   # channel where the Invite Tracker app posts join/leave messages
INVITE_LOG_CHANNEL_ID = 1535819132287717476       # /syncinvites and the live listener keep an auto-updated JSON "database" message here
TOURNAMENT_PANEL_CHANNEL_ID = 1528515043992404150  # the tournament team-select panel is posted/refreshed here
TOURNAMENT_SUBMISSION_ROLE_ID = 1533580965094359211  # granted to everyone signed up for the tournament
TOURNAMENT_CLEAR_PURGE_CHANNEL_ID = 1533581676184076398  # fully purged when the panel's Clear button is used
TOURNAMENT_SIGNUP_CAP = 7                  # max sign-ups per team for the sticky tournament message
TOURNAMENT_STICKY_DEBOUNCE_SECONDS = 5     # min gap between re-sticking a team's sign-up message, per channel

# ---------- Lurkr level role sync ----------
# Thresholds checked highest-first: a member's target role is the first one whose
# level requirement they meet or exceed. Update this list if the Lurkr level-role
# rewards ever change.
LEVEL_ROLE_THRESHOLDS = [
    (100, 1541608490345435227),
    (90, 1541608406199042108),
    (80, 1541608296123998318),
    (70, 1541608190473666670),
    (60, 1541608081107066891),
    (50, 1541599434419470416),
    (40, 1541599352076632205),
    (30, 1541599279095877632),
    (20, 1528221409317486713),
    (15, 1528490625777926228),
    (10, 1528161334632644868),
    (6, 1528161296183463966),
    (5, 1528161220924932136),
    (4, 1528161147541520416),
    (3, 1528160499676614657),
    (2, 1528160422857932868),
    (1, 1528160287910264973),
]
LEVEL_ROLE_IDS = {role_id for _level, role_id in LEVEL_ROLE_THRESHOLDS}


def target_level_role_ids(level: int) -> set[int]:
    """Every level role a member at this level should hold — stacked, not just the
    highest one (e.g. level 45 gets the Level 1/2/3/4/5/6/10/15/20/30/40 roles)."""
    return {role_id for threshold, role_id in LEVEL_ROLE_THRESHOLDS if level >= threshold}

TEAM_CHANNEL_FULL_ACCESS_ROLE_ID = 1528155138337013921  # legacy — no longer granted access; kept only so sync_existing_teams can strip its old overwrites
TEAM_CHANNEL_VIEWER_ROLE_ID = 1535819394129854474  # legacy — no longer granted access; kept only so sync_existing_teams can strip its old overwrites
TEAM_CHANNEL_BASIC_ACCESS_ROLE_ID = 1539098236772425830  # gets basic (view/send/read history/react + ping-any-role) access in every team channel, except TEAM_CHANNEL_BASIC_ACCESS_EXCLUDED_CHANNEL_ID
TEAM_CHANNEL_BASIC_ACCESS_EXCLUDED_CHANNEL_ID = 1534638919465963570  # this one team channel is skipped when granting TEAM_CHANNEL_BASIC_ACCESS_ROLE_ID access

QOTD_CHANNEL_ID = 1535123663844548639       # where /qotd posts the question and opens its thread
QOTD_PING_ROLE_ID = 1535432839548506163     # pinged alongside each Question of the Day

SCRIM_CATEGORY_ID = 1537952629131444244     # category /startscrim channels are created in
SCRIM_LOG_CHANNEL_ID = 1537897056218386462  # scrims JSON "database" message lives here
SCRIM_CHANNEL_NAME = "🎯┃scrim"              # name given to every scrim channel
SCRIM_DURATION_DAYS = 3                     # scrim channels auto-delete this many days after creation
SCRIM_CHECK_INTERVAL_MINUTES = 5            # how often the expiry loop checks for scrim channels to delete

# ---------- Data privacy panel (Toggle Activity Tracking / Delete My Data) ----------
BOT_DISPLAY_NAME = "Arena Hub Bot"  # shown in the privacy panel's copy
PRIVACY_PANEL_CHANNEL_ID = 1528230357072347146   # the privacy panel is posted/refreshed here
DATA_DELETE_REQUEST_CHANNEL_ID = 1544698404574072842  # "Delete My Data" requests are sent here for staff to approve/deny
DATA_OPTOUT_LOG_CHANNEL_ID = 1544739792611319858  # activity-tracking opt-out JSON "database" message lives here
DATA_OPTOUT_DB_FILE = "data_optout_data.json"

TEAM_LEAVE_EMOJI = "<:Capybara:1528229276254470144>"  # posted in the team channel when someone leaves/is kicked

TEAMS_DB_FILE = "teams_data.json"
GIVEAWAYS_DB_FILE = "giveaways_data.json"
TICKETS_DB_FILE = "tickets_data.json"
INVITES_DB_FILE = "invites_data.json"
SCRIMS_DB_FILE = "scrims_data.json"
DB_FILE = "teams.json"  # legacy combined file — read only, for one-time migration
# (DATA_OPTOUT_DB_FILE is declared above, next to the privacy-panel channel config it belongs with)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SUPPORT_BANNER_PATH = os.path.join(BASE_DIR, "support_banner.png")
SUPPORT_BANNER_FILENAME = "support_banner.png"

# ---------- Meta Quest update tracker config ----------
META_UPDATE_CHANNEL_ID = 1528008387420356629  # where update announcements are posted
META_LOG_CHANNEL_ID = 1535478538776608859     # last-logged-version JSON "database" message lives here
META_URL = "https://www.meta.com/experiences/animal-company/7190422614401072/"
META_VERSION_FILE = os.path.join(BASE_DIR, "lastMetaVersion.txt")  # legacy plaintext file — read only, for one-time migration
META_VERSION_DB_FILE = "meta_version_data.json"
META_CHECK_INTERVAL_MINUTES = 5  # how often to auto-check for a new version
META_GAME_DISPLAY_NAME = "Wooster Games, Animal Company"  # bold subtitle line shown on the update embed
META_EMBED_AUTHOR = "AC: Arena Hub"  # small eyebrow text shown above the embed title
META_UPDATE_PING_ROLE_ID = 1528140472051040307  # pinged whenever a real update is detected

# ---------- Weekly message leaderboard config ----------
MESSAGE_LEADERBOARD_LOG_CHANNEL_ID = 1539159955922362369  # channel the leaderboard's JSON backup lives in
MESSAGE_LEADERBOARD_DB_FILE = "message_leaderboard_data.json"
MESSAGE_LEADERBOARD_SAVE_INTERVAL_MINUTES = 5   # how often in-memory counts are flushed to disk + backed up
MESSAGE_LEADERBOARD_RESET_WEEKDAY = 6           # Python weekday(): Monday=0 ... Sunday=6
MESSAGE_LEADERBOARD_RESET_HOUR_UTC = 23         # counts reset every Sunday at this hour, UTC
MESSAGE_LEADERBOARD_RESET_MINUTE_UTC = 59       # ...and this minute — i.e. Sunday 23:59 UTC
MESSAGE_LEADERBOARD_TRACKED_CHANNEL_ID = 1528008744422871211  # only messages sent here count
MESSAGE_COUNT_SPAM_COOLDOWN_SECONDS = 5  # a user's message only counts toward either leaderboard if this long has passed since their last counted one
MESSAGE_LEADERBOARD_TOP3_ROLE_ID = 1538743573103648788   # given to #1-#3 at each weekly reset
MESSAGE_LEADERBOARD_TOP10_ROLE_ID = 1542340814418743406  # given to #1-#10 at each weekly reset (top 3 get both)

# ---------- All-time (never resets) message leaderboard config ----------
OVERALL_MESSAGE_LOG_CHANNEL_ID = 1542639777809965167  # channel the all-time JSON backup lives in
OVERALL_MESSAGE_DB_FILE = "overall_message_data.json"
OVERALL_MESSAGE_SAVE_INTERVAL_MINUTES = 5  # how often in-memory counts are flushed to disk + backed up

# ---------- Chat streak config ----------
STREAK_ANNOUNCE_CHANNEL_ID = 1528015385511723079  # gained/lost streak messages are posted here
STREAK_LOG_CHANNEL_ID = 1545181057857355857        # streak JSON "database" message lives here
STREAK_DB_FILE = "streak_data.json"
STREAK_MESSAGES_REQUIRED = 3        # messages needed within the window to bank the day's streak
STREAK_WINDOW_HOURS = 24            # length of each streak window
STREAK_REVIVE_WINDOW_HOURS = 24     # how long after losing a streak /revivestreak still works
STREAK_REVIVE_MONTHLY_LIMIT = 1     # /revivestreak uses allowed per user per calendar month
STREAK_CHECK_INTERVAL_MINUTES = 5   # how often the loop checks for expired windows/revive grace periods
STREAK_LEAVE_EMOJI = "💔"
STREAK_GAIN_EMOJI = "🔥"

# Chat-streak milestone roles: stacked, not just the highest (a 14-day streak holds the
# 1/3/7/14-day roles too) — same pattern as LEVEL_ROLE_THRESHOLDS above. Wiped entirely the
# moment a streak is lost (see _advance_streak_phase), and re-granted automatically if the
# streak is revived via /revivestreak since streak_days survives a revive.
STREAK_ROLE_THRESHOLDS = [
    (30, 1545191707044352081),
    (14, 1545191647057412227),
    (7, 1545191607500935248),
    (3, 1545191558846881892),
    (1, 1545191515217727589),
]
STREAK_ROLE_IDS = {role_id for _days, role_id in STREAK_ROLE_THRESHOLDS}


def target_streak_role_ids(streak_days: int, phase: str) -> set[int]:
    """Every streak role a member should currently hold — stacked, and none at all once the
    streak is lost (phase == "lost") or the count is back at zero."""
    if phase == "lost" or streak_days <= 0:
        return set()
    return {role_id for threshold, role_id in STREAK_ROLE_THRESHOLDS if streak_days >= threshold}

# Image-card rendering (ported from the old discord.js leaderboard.js design) — assets live
# next to this file: two Orbitron weights and an optional background image. Falls back to a
# plain dark canvas if the assets aren't present, so the command still works out of the box.
LEADERBOARD_BG_PATH = os.path.join(BASE_DIR, "leaderboard-bg.png")
LEADERBOARD_BG_FALLBACK_PATH = os.path.join(BASE_DIR, "default-background.png")
LEADERBOARD_FONT_PATH = os.path.join(BASE_DIR, "Orbitron-Bold.ttf")        # family "Orbitron" in the JS version
LEADERBOARD_FONT_EXTRABOLD_PATH = os.path.join(BASE_DIR, "Orbitron-ExtraBold.ttf")  # family "OrbitronExtra"
LEADERBOARD_CANVAS_WIDTH = 900
LEADERBOARD_CANVAS_HEIGHT = 520
LEADERBOARD_COOLDOWN_SECONDS = 300  # how long a user must wait between /messageleaderboard runs


# ---------- Bot setup ----------
intents = discord.Intents.default()
intents.members = True  # needed to reliably resolve members / add roles
intents.message_content = True  # needed to read Invite Tracker's messages in INVITE_TRACKER_CHANNEL_ID

bot = commands.Bot(command_prefix="!", intents=intents)


# ---------- JSON "database" helpers ----------
# The team data and giveaway data live in two separate local files (and are backed up to
# two separate Discord channels — see backup_db_to_log_channel / restore_db_from_log_channel
# below), but the rest of the bot still works with a single merged {"teams": ..., "giveaways": ...}
# dict in memory, exactly as before, so nothing else in the file needs to change.

def _load_json_file(path: str):
    """Reads a JSON file, returning None if it's missing OR present-but-invalid
    (e.g. empty/truncated because the process was killed mid-write). Callers should
    treat None the same as "no file" rather than letting the exception propagate —
    an empty file on disk should never be able to crash startup."""
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f"[WARN] {path} exists but isn't valid JSON ({e}); treating as missing.")
        return None


def _atomic_write_json(path: str, data: dict) -> None:
    """Writes JSON to path atomically (write to a temp file, then os.replace) so a
    crash/kill mid-write can never leave a truncated/empty file behind — os.replace
    is atomic on POSIX and Windows, unlike open(path, 'w') which truncates in place."""
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp_path, path)


def load_db() -> dict:
    data = {"teams": {}, "giveaways": {}}

    teams_file = _load_json_file(TEAMS_DB_FILE)
    if teams_file is not None:
        if "teams" not in teams_file:
            # migrate old flat-format {team_name: {...}} files
            teams_file = {"teams": teams_file}
        data["teams"] = teams_file.get("teams", {})

    giveaways_file = _load_json_file(GIVEAWAYS_DB_FILE)
    if giveaways_file is not None:
        data["giveaways"] = giveaways_file.get("giveaways", {})

    return data


def save_db(data: dict) -> None:
    now = discord.utils.utcnow().isoformat()
    _atomic_write_json(TEAMS_DB_FILE, {"teams": data.get("teams", {}), "last_updated": now})
    _atomic_write_json(GIVEAWAYS_DB_FILE, {"giveaways": data.get("giveaways", {}), "last_updated": now})


# Cache of each database message (keyed by channel ID) so we edit it in place instead of
# posting a new file every time. Populated lazily by scanning channel history.
_db_message_cache: dict = {}

# Per-channel lock so two backups firing at nearly the same time (e.g. two team actions
# completing back-to-back) can't both miss _db_message_cache at once and each post a brand
# new "database" message — without this, concurrent saves race and duplicates pile up in
# the channel instead of one message being edited in place.
_db_message_locks: dict = {}


def _get_db_message_lock(channel_id: int) -> asyncio.Lock:
    lock = _db_message_locks.get(channel_id)
    if lock is None:
        lock = asyncio.Lock()
        _db_message_locks[channel_id] = lock
    return lock

# Set by the giveaway Join/Leave button when entries change; flushed by check_giveaways()
# every 30s instead of backing up on every single click — see the comment in
# GiveawayView._make_join_callback() for why.
_giveaway_backup_dirty: bool = False

# User IDs with a /createteam request currently awaiting admin confirmation,
# so the same user can't queue up multiple pending requests.
pending_team_requests: set = set()

# Leader user ID -> {"team": ..., "invited_user_id": ..., "dm_channel_id": ..., "dm_message_id": ...}
# for whichever invite (if any) that leader currently has outstanding. Used to stop a
# leader from having more than one invite pending at the same time.
pending_invites: dict = {}

# Challenger leader user ID -> {"opponent_team": ..., "opponent_leader_id": ..., "dm_channel_id": ...,
# "dm_message_id": ...} for whichever /startscrim request (if any) that leader currently has
# outstanding. Used to stop a leader from having more than one scrim request pending at once.
pending_scrim_requests: dict = {}

# Set once the first on_ready pass has fully finished, so if Discord fires on_ready again
# on a later reconnect (same process, no actual restart), we don't log a spurious "restarted".
_startup_logged: bool = False

# User IDs who've opted OUT of activity tracking via the privacy panel's "Toggle Activity
# Tracking" button. Everyone is tracked by default (i.e. absence from this set = tracked).
# Kept in memory for a cheap check on every message; the source of truth is
# DATA_OPTOUT_DB_FILE, loaded on startup by restore_optout_db_from_log_channel().
_data_optout_ids: set = set()

# User IDs with a "Delete My Data" request currently awaiting staff approval in
# DATA_DELETE_REQUEST_CHANNEL_ID, so the same user can't queue up multiple pending requests.
pending_data_delete_requests: set = set()

# In-memory weekly message-leaderboard counters: {user_id: message_count}. Loaded on startup
# from its backup channel (restore_message_leaderboard_from_log_channel) and periodically
# flushed to disk + Discord by message_leaderboard_save_loop, rather than hitting the
# filesystem on every single message sent in the server.
_message_leaderboard_counts: dict = {}
_message_leaderboard_period_start: datetime | None = None
_message_leaderboard_dirty: bool = False

# In-memory ALL-TIME message counters: {user_id: message_count}. Same load/flush pattern as
# the weekly ones above, but counts every non-bot message sent anywhere in the guild and
# never resets — see OVERALL_MESSAGE_LOG_CHANNEL_ID.
_overall_message_counts: dict = {}
_overall_message_dirty: bool = False

# In-memory chat-streak state: {user_id: {"streak_days": int, "phase": "counting" | "waiting"
# | "lost", "phase_end": iso str | None, "messages_in_window": int, "revive_month": "YYYY-MM"
# str | None}}. Same load/flush pattern as the other trackers — see
# restore_streak_db_from_log_channel() and streak_check_loop below.
_streak_data: dict = {}
_streak_dirty: bool = False

# Per-user cooldown gate shared by BOTH leaderboards: {user_id: last counted message's time.time()}.
# A message only counts toward either leaderboard if MESSAGE_COUNT_SPAM_COOLDOWN_SECONDS has
# passed since that user's last counted message — stops someone inflating their count (on
# either leaderboard) by spamming short messages back-to-back. This does NOT block the
# messages themselves, only whether they're counted.
_message_count_cooldowns: dict = {}


async def _get_or_create_db_message(channel_id: int, filename: str):
    if channel_id in _db_message_cache:
        return _db_message_cache[channel_id]

    channel = bot.get_channel(channel_id) or await bot.fetch_channel(channel_id)

    # Collect every one of the bot's own messages carrying this filename, not just the
    # first one found. If duplicates already exist in the channel (from before this fix,
    # or a past race/hiccup), keep only the newest and delete the rest so the channel
    # settles back down to a single database message instead of accumulating more.
    matches = []
    async for msg in channel.history(limit=200):
        if msg.author.id == bot.user.id and msg.attachments and msg.attachments[0].filename == filename:
            matches.append(msg)

    if not matches:
        return None

    # channel.history() yields newest-first by default, so the first match is the one to keep.
    keep, duplicates = matches[0], matches[1:]
    for dup in duplicates:
        try:
            await dup.delete()
        except discord.HTTPException:
            pass
    if duplicates:
        print(f"[INFO] Cleaned up {len(duplicates)} duplicate `{filename}` message(s) in channel {channel_id}.")

    _db_message_cache[channel_id] = keep
    return keep


async def _backup_file_to_channel(channel_id: int, file_path: str, filename: str):
    """Keeps a single message in the given channel updated with the given local file,
    editing it in place rather than posting a new file every time."""
    async with _get_db_message_lock(channel_id):
        channel = bot.get_channel(channel_id) or await bot.fetch_channel(channel_id)
        with open(file_path, "rb") as f:
            file_bytes = f.read()
        new_file = discord.File(io.BytesIO(file_bytes), filename=filename)

        msg = await _get_or_create_db_message(channel_id, filename)
        if msg is not None:
            try:
                edited = await msg.edit(content="📦 Database (auto-updated):", attachments=[new_file])
                _db_message_cache[channel_id] = edited
                await log_bot_event(
                    title="📦 JSON synced",
                    description=f"`{filename}` backed up to <#{channel_id}>.",
                    colour=discord.Colour.dark_teal(),
                )
                return
            except discord.HTTPException:
                pass  # message may have been deleted; fall through and send a fresh one

        sent = await channel.send(content="📦 Database (auto-updated):", file=new_file)
        _db_message_cache[channel_id] = sent
        await log_bot_event(
            title="📦 JSON synced",
            description=f"`{filename}` backed up to <#{channel_id}> (new message).",
            colour=discord.Colour.dark_teal(),
        )


async def backup_db_to_log_channel():
    """Keeps the teams file backed up in TEAM_LOG_CHANNEL_ID and the giveaways file backed
    up in GIVEAWAY_LOG_CHANNEL_ID, each as its own auto-updated message."""
    await _backup_file_to_channel(TEAM_LOG_CHANNEL_ID, TEAMS_DB_FILE, TEAMS_DB_FILE)
    await _backup_file_to_channel(GIVEAWAY_LOG_CHANNEL_ID, GIVEAWAYS_DB_FILE, GIVEAWAYS_DB_FILE)


async def log_team_event(
    title: str,
    description: str = "",
    colour: discord.Colour = discord.Colour.blurple(),
    fields: list[tuple[str, str, bool]] | None = None,
) -> None:
    """Sends one standardized embed to TEAM_ACTIVITY_LOG_CHANNEL_ID for every team-related
    event — creation, deletion, joins, leaves, invites (sent/accepted/declined/cancelled/
    expired/failed), kicks, force-adds, leader promotions, and setting changes. This is a
    human-readable activity feed, separate from the raw JSON "database" backup kept in
    TEAM_LOG_CHANNEL_ID. Fire-and-forget: any failure here is printed and swallowed so a
    logging hiccup never breaks the actual team action that triggered it."""
    try:
        channel = bot.get_channel(TEAM_ACTIVITY_LOG_CHANNEL_ID) or await bot.fetch_channel(TEAM_ACTIVITY_LOG_CHANNEL_ID)
        if channel is None:
            return
        embed = discord.Embed(
            title=title,
            description=description or None,
            colour=colour,
            timestamp=discord.utils.utcnow(),
        )
        for name, value, inline in (fields or []):
            embed.add_field(name=name, value=value or "—", inline=inline)
        await channel.send(embed=embed)
    except Exception as e:
        print(f"[ERROR] Failed to send team activity log: {e}")


async def log_bot_event(
    title: str,
    description: str = "",
    colour: discord.Colour = discord.Colour.blurple(),
    fields: list[tuple[str, str, bool]] | None = None,
) -> None:
    """Sends one standardized embed to BOT_LOG_CHANNEL_ID — the global bot activity log.
    Covers every slash command run (via on_app_command_completion), every JSON db sync
    (backup/restore, hooked in _backup_file_to_channel / _restore_file_from_channel below,
    so it automatically covers teams, giveaways, tickets, invites, scrims, and the meta
    version file), and startup/restart (on_ready). Fire-and-forget: any failure here is
    printed and swallowed so a logging hiccup never breaks the actual action that triggered
    it."""
    try:
        channel = bot.get_channel(BOT_LOG_CHANNEL_ID) or await bot.fetch_channel(BOT_LOG_CHANNEL_ID)
        if channel is None:
            return
        embed = discord.Embed(
            title=title,
            description=description or None,
            colour=colour,
            timestamp=discord.utils.utcnow(),
        )
        for name, value, inline in (fields or []):
            embed.add_field(name=name, value=value or "—", inline=inline)
        await channel.send(embed=embed)
    except Exception as e:
        print(f"[ERROR] Failed to send bot log: {e}")


async def _restore_file_from_channel(channel_id: int, file_path: str, filename: str) -> bool:
    """Looks for filename attached to one of the bot's own messages in channel_id and, if
    found, writes it to file_path. Returns whether a backup was found."""
    async with _get_db_message_lock(channel_id):
        try:
            channel = bot.get_channel(channel_id) or await bot.fetch_channel(channel_id)
            async for msg in channel.history(limit=200):
                if msg.author.id == bot.user.id and msg.attachments and msg.attachments[0].filename == filename:
                    data = await msg.attachments[0].read()
                    with open(file_path, "wb") as f:
                        f.write(data)
                    _db_message_cache[channel_id] = msg
                    await log_bot_event(
                        title="📥 JSON restored",
                        description=f"`{filename}` restored from <#{channel_id}>.",
                        colour=discord.Colour.dark_teal(),
                    )
                    return True
        except discord.HTTPException as e:
            print(f"Failed to restore {filename} from channel {channel_id}: {e}")
    return False


async def restore_db_from_log_channel():
    """Pulls the last known team/giveaway backups from their dedicated log channels into
    local storage. Critical because Railway wipes the container's disk on every redeploy —
    without this, every restart would silently start from an empty database even though a
    good backup is sitting in Discord.

    One-time migration: the very first time this runs after the teams/giveaways split,
    neither dedicated channel has a backup yet. In that case we fall back to the old
    combined log channel (LOG_CHANNEL_ID), split whatever's there into the two new files,
    and immediately push fresh backups to the two new channels so no previous data is lost."""
    # Use validity, not mere existence — a truncated/empty file left behind by a
    # process that died mid-write (see _atomic_write_json) must not be mistaken for
    # a good local copy, or we'd skip restoring the real backup from Discord.
    have_teams = _load_json_file(TEAMS_DB_FILE) is not None
    have_giveaways = _load_json_file(GIVEAWAYS_DB_FILE) is not None

    if not have_teams:
        have_teams = await _restore_file_from_channel(TEAM_LOG_CHANNEL_ID, TEAMS_DB_FILE, TEAMS_DB_FILE)
    if not have_giveaways:
        have_giveaways = await _restore_file_from_channel(GIVEAWAY_LOG_CHANNEL_ID, GIVEAWAYS_DB_FILE, GIVEAWAYS_DB_FILE)

    if have_teams and have_giveaways:
        print("Restored database from the team/giveaway log channels.")
        return

    # Fall back to the old combined backup and split it into the two new files.
    legacy = _load_json_file(DB_FILE)
    if legacy is None:
        try:
            channel = bot.get_channel(LOG_CHANNEL_ID) or await bot.fetch_channel(LOG_CHANNEL_ID)
            async for msg in channel.history(limit=50):
                if msg.author.id == bot.user.id and msg.attachments and msg.attachments[0].filename == DB_FILE:
                    raw = await msg.attachments[0].read()
                    legacy = json.loads(raw.decode("utf-8"))
                    break
        except discord.HTTPException as e:
            print(f"Failed to check legacy combined log channel: {e}")

    if legacy is None:
        if not have_teams and not have_giveaways:
            print("No existing database backup found in any log channel — starting fresh.")
        return

    if "teams" not in legacy:
        legacy = {"teams": legacy}  # migrate very old flat-format files too

    migrated_at = discord.utils.utcnow().isoformat()

    if not have_teams:
        _atomic_write_json(TEAMS_DB_FILE, {"teams": legacy.get("teams", {}), "last_updated": migrated_at})
        print("Migrated teams data from the legacy combined log channel.")
    if not have_giveaways:
        _atomic_write_json(GIVEAWAYS_DB_FILE, {"giveaways": legacy.get("giveaways", {}), "last_updated": migrated_at})
        print("Migrated giveaways data from the legacy combined log channel.")

    # Push the split data into the two new dedicated channels right away, rather than
    # waiting for the next command that happens to save.
    await backup_db_to_log_channel()


# ---------- Tickets (opened from the support panel) ----------
# Each opened ticket gets a sequential number and a thread; the counter and a record of
# every ticket (thread ID -> number/opener/category/closed state) live in TICKETS_DB_FILE
# and are backed up as an auto-updated message in TICKET_LOG_CHANNEL_ID, the same way
# teams/giveaways are.
def load_ticket_db() -> dict:
    data = _load_json_file(TICKETS_DB_FILE)
    if data is None:
        return {"next_number": 1, "tickets": {}, "panel_message_id": None}
    data.setdefault("next_number", 1)
    data.setdefault("tickets", {})
    data.setdefault("panel_message_id", None)
    return data


def save_ticket_db(data: dict) -> None:
    data["last_updated"] = discord.utils.utcnow().isoformat()
    _atomic_write_json(TICKETS_DB_FILE, data)


async def backup_ticket_db_to_log_channel():
    try:
        await _backup_file_to_channel(TICKET_LOG_CHANNEL_ID, TICKETS_DB_FILE, TICKETS_DB_FILE)
    except discord.HTTPException as e:
        print(f"Failed to back up ticket db to log channel: {e}")
    except Exception as e:  # noqa: BLE001 - never let a bad backup attempt kill a command
        print(f"Unexpected error backing up ticket db: {e}")


async def restore_ticket_db_from_log_channel():
    # Validity, not mere existence — a broken local file must not (a) crash load_ticket_db
    # later, or (b) get pushed to the log channel here and clobber a good backup.
    if _load_json_file(TICKETS_DB_FILE) is not None:
        # Local data already present (e.g. a crash-restart, not a fresh container) — push
        # it straight to the log channel so the backup there is confirmed up to date.
        await backup_ticket_db_to_log_channel()
        return
    try:
        found = await _restore_file_from_channel(TICKET_LOG_CHANNEL_ID, TICKETS_DB_FILE, TICKETS_DB_FILE)
    except discord.HTTPException as e:
        print(f"Failed to restore ticket db from log channel: {e}")
        return
    if found:
        print("Restored ticket db from log channel backup.")
    else:
        print("No existing ticket db backup found — starting fresh.")


# ---------- Data privacy (activity-tracking opt-out / delete-my-data) ----------
# Backed by DATA_OPTOUT_DB_FILE, backed up as an auto-updated message in
# DATA_OPTOUT_LOG_CHANNEL_ID the same way teams/tickets/etc. are. "opted_out" holds the
# IDs of users who've opted out of activity tracking (everyone else is tracked by
# default). "deleted_users" is just a bare marker — not the data itself — kept so we can
# confirm to someone that a deletion already happened if they ask again later, per the
# privacy panel's copy.
def load_optout_db() -> dict:
    data = _load_json_file(DATA_OPTOUT_DB_FILE)
    if data is None:
        return {"opted_out": [], "deleted_users": [], "panel_message_id": None}
    data.setdefault("opted_out", [])
    data.setdefault("deleted_users", [])
    data.setdefault("panel_message_id", None)
    return data


def save_optout_db(data: dict) -> None:
    data["last_updated"] = discord.utils.utcnow().isoformat()
    _atomic_write_json(DATA_OPTOUT_DB_FILE, data)


def _sync_optout_cache(data: dict) -> None:
    """Refreshes the in-memory _data_optout_ids cache from a loaded db dict, so the
    per-message tracking check doesn't have to hit the filesystem every time."""
    _data_optout_ids.clear()
    _data_optout_ids.update(int(uid) for uid in data.get("opted_out", []))


async def backup_optout_db_to_log_channel():
    try:
        await _backup_file_to_channel(DATA_OPTOUT_LOG_CHANNEL_ID, DATA_OPTOUT_DB_FILE, DATA_OPTOUT_DB_FILE)
    except discord.HTTPException as e:
        print(f"Failed to back up data-optout db to log channel: {e}")
    except Exception as e:  # noqa: BLE001 - never let a bad backup attempt kill a command
        print(f"Unexpected error backing up data-optout db: {e}")


async def restore_optout_db_from_log_channel():
    if _load_json_file(DATA_OPTOUT_DB_FILE) is not None:
        _sync_optout_cache(load_optout_db())
        await backup_optout_db_to_log_channel()
        return
    try:
        found = await _restore_file_from_channel(DATA_OPTOUT_LOG_CHANNEL_ID, DATA_OPTOUT_DB_FILE, DATA_OPTOUT_DB_FILE)
    except discord.HTTPException as e:
        print(f"Failed to restore data-optout db from log channel: {e}")
        return
    if found:
        _sync_optout_cache(load_optout_db())
        print("Restored data-optout db from log channel backup.")
    else:
        print("No existing data-optout db backup found — starting fresh (everyone tracked by default).")


async def purge_user_activity_data(user_id: int) -> str:
    """Removes user_id's tracked activity data — the weekly/all-time message-leaderboard
    counts and their invite-tracker record — from the JSON stores, for a "Delete My Data"
    request approved by staff. Deliberately leaves team membership, tickets, scrims, and
    giveaway entries alone: those are structural/roles-based records tied to features the
    person is still using, not activity tracking, matching the privacy panel's "This does
    NOT include ... roles you currently hold" wording."""
    global _message_leaderboard_dirty, _overall_message_dirty, _streak_dirty
    removed = []

    if _message_leaderboard_counts.pop(user_id, None) is not None:
        removed.append("weekly message-leaderboard count")
        _message_leaderboard_dirty = False
        save_message_leaderboard_db()
        await backup_message_leaderboard_to_log_channel()

    if _overall_message_counts.pop(user_id, None) is not None:
        removed.append("all-time message count")
        _overall_message_dirty = False
        save_overall_message_db()
        await backup_overall_message_to_log_channel()

    _message_count_cooldowns.pop(user_id, None)

    if _streak_data.pop(user_id, None) is not None:
        removed.append("chat streak")
        _streak_dirty = False
        save_streak_db()
        await backup_streak_db_to_log_channel()

    invite_db = load_invite_db()
    invited_users = invite_db.get("invited_users", {})
    stale_keys = [
        key for key, record in invited_users.items()
        if record.get("user_id") == user_id or key == str(user_id)
    ]
    for key in stale_keys:
        del invited_users[key]
    if stale_keys:
        removed.append(f"{len(stale_keys)} invite-tracker record(s)")
        save_invite_db(invite_db)
        await backup_invite_db_to_log_channel()

    if not removed:
        return "No stored activity data was found for this user."
    return "Removed: " + ", ".join(removed) + "."


def find_team_by_leader(db: dict, user_id: int):
    for name, info in db.items():
        if info["leader_id"] == user_id:
            return name
    return None


def find_team_by_leader_or_coleader(db: dict, user_id: int):
    """Same as find_team_by_leader, but also matches a team's co-leader (set via
    /setcoleader) — used to gate the commands co-leaders are allowed to use too."""
    for name, info in db.items():
        if info["leader_id"] == user_id or info.get("co_leader_id") == user_id:
            return name
    return None


def find_team_by_member(db: dict, user_id: int):
    for name, info in db.items():
        if user_id in info.get("members", []):
            return name
    return None


def find_team_by_channel(db: dict, channel_id: int):
    for name, info in db.items():
        if info.get("channel_id") == channel_id:
            return name
    return None


def find_team_by_role_id(db: dict, role_id: int):
    for name, info in db.items():
        if info.get("role_id") == role_id:
            return name
    return None


def find_team_key_ci(db: dict, name: str):
    name_lower = name.lower()
    for key in db:
        if key.lower() == name_lower:
            return key
    return None


def record_team_join(info: dict, user_id: int) -> None:
    """Stamps the current time as when user_id joined this team, stored in the team's own
    'joined_at' map (user_id -> ISO timestamp) inside the team database. Used to enforce the
    TEAM_JOIN_COOLDOWN_DAYS wait before a member can leave and join a different team."""
    info.setdefault("joined_at", {})[str(user_id)] = discord.utils.utcnow().isoformat()


def clear_team_join(info: dict, user_id: int) -> None:
    """Removes a member's join-date record, e.g. once they've left or been kicked."""
    info.get("joined_at", {}).pop(str(user_id), None)


def get_team_leave_eligible_ts(info: dict, user_id: int):
    """Returns the unix timestamp at which user_id becomes free to leave this team (i.e.
    TEAM_JOIN_COOLDOWN_DAYS after they joined), or None if there's no join date on record
    (e.g. teams/members that existed before this feature was added)."""
    joined_at_raw = info.get("joined_at", {}).get(str(user_id))
    if not joined_at_raw:
        return None
    try:
        joined_at = datetime.fromisoformat(joined_at_raw)
    except ValueError:
        return None
    return int((joined_at + timedelta(days=TEAM_JOIN_COOLDOWN_DAYS)).timestamp())


def is_valid_standard_emoji(text: str) -> bool:
    """True only for a single standard/unicode Discord emoji (no custom server emoji,
    no plain text) — custom emoji can't be used in channel names or as role icons this way."""
    return emoji_lib.is_emoji(text)


def normalize_hex_colour(text: str):
    """Returns a '#RRGGBB' string if valid, else None."""
    if re.fullmatch(r"#?[0-9A-Fa-f]{6}", text.strip()):
        cleaned = text.strip().lstrip("#")
        return f"#{cleaned}"
    return None


def has_staff_role(member: discord.Member) -> bool:
    return any(role.id == STAFF_ROLE_ID for role in member.roles)


def has_premium_access(member: discord.Member) -> bool:
    return any(role.id in (PREMIUM_ROLE_ID, PREMIUM_ROLE_ID_2, STAFF_ROLE_ID) for role in member.roles)


async def safe_edit_original_response(interaction: discord.Interaction, content: str, view=None) -> None:
    """Edits the interaction's original response, falling back to a fresh ephemeral
    followup message if the original response can no longer be found (e.g. it expired,
    was dismissed, or was otherwise removed) — avoids crashing view callbacks with an
    unhandled discord.NotFound."""
    try:
        await interaction.edit_original_response(content=content, view=view)
    except discord.HTTPException:
        try:
            await interaction.followup.send(content=content, ephemeral=True)
        except discord.HTTPException:
            pass


def build_team_leave_message(user_id: int) -> str:
    """Posted in the team channel whenever a member leaves or is kicked from the team."""
    return f"{TEAM_LEAVE_EMOJI} <@{user_id}>, just left the team"


def build_team_welcome_message(user_id: int, role_id: int) -> str:
    """Posted in a team's channel right after it's created, welcoming the new leader."""
    return (
        f"Hello <@{user_id}> !\n"
        f"You are now, the leader of <@&{role_id}> .\n"
        f"**__List of commands:__**\n"
        f"`/invite`\n"
        f"`/changeteamsettings`\n"
        f"`/premiumteamsettings`\n"
        f"`/leaderpromote`"
    )


def team_leader_channel_overwrite() -> discord.PermissionOverwrite:
    """Permissions granted to a team leader in their own team channel: on top of viewing/
    sending, manage_messages lets them delete and pin messages, and mention_everyone lets
    them ping their team's role even though the role itself isn't set to be mentionable.
    Co-leaders (see /setcoleader) get this exact same overwrite."""
    return discord.PermissionOverwrite(
        view_channel=True,
        send_messages=True,
        manage_messages=True,
        mention_everyone=True,
    )


async def _clear_team_coleader(db: dict, team_name: str, guild: discord.Guild, reason: str) -> None:
    """Clears a team's co-leader slot (if any) and reverts their channel permission
    overwrite back to whatever the base team role alone grants. Saves + backs up db.
    Used both by /setcoleader itself and whenever a co-leader is kicked/leaves the team
    outright, so a stale co-leader ID never lingers with elevated channel permissions."""
    info = db["teams"].get(team_name)
    if info is None:
        return
    old_co_leader_id = info.get("co_leader_id")
    if old_co_leader_id is None:
        return

    info["co_leader_id"] = None
    save_db(db)
    await backup_db_to_log_channel()

    channel = guild.get_channel(info.get("channel_id"))
    old_member = guild.get_member(old_co_leader_id)
    if channel is not None and old_member is not None:
        try:
            await channel.set_permissions(old_member, overwrite=None, reason=reason)
        except discord.HTTPException:
            pass

    if old_member is not None:
        co_leader_role = guild.get_role(TEAM_CO_LEADER_ROLE_ID)
        if co_leader_role is not None and co_leader_role in old_member.roles:
            try:
                await old_member.remove_roles(co_leader_role, reason=reason)
            except discord.HTTPException:
                pass


def team_channel_basic_access_overwrite() -> discord.PermissionOverwrite:
    """Basic-use permissions, allowed — granted to TEAM_CHANNEL_BASIC_ACCESS_ROLE_ID in
    every team channel except TEAM_CHANNEL_BASIC_ACCESS_EXCLUDED_CHANNEL_ID. Lets holders
    view the channel, send messages, read history, and react (i.e. use basic commands)
    without any channel/message/permission management rights. mention_everyone is included
    so holders can still ping any role in the channel, even one that isn't itself set to be
    mentionable — same trick used for team leaders."""
    return discord.PermissionOverwrite(
        view_channel=True,
        send_messages=True,
        read_message_history=True,
        add_reactions=True,
        use_application_commands=True,
        mention_everyone=True,
    )


# Preset palette offered in /premiumteamsettings' colour1/colour2 dropdowns (Discord caps choices at 25).
PREMIUM_COLOUR_CHOICES = [
    app_commands.Choice(name="Red", value="#ED4245"),
    app_commands.Choice(name="Crimson", value="#DC143C"),
    app_commands.Choice(name="Maroon", value="#800000"),
    app_commands.Choice(name="Orange", value="#E67E22"),
    app_commands.Choice(name="Coral", value="#FF7F50"),
    app_commands.Choice(name="Gold", value="#F1C40F"),
    app_commands.Choice(name="Yellow", value="#FEE75C"),
    app_commands.Choice(name="Lime", value="#32CD32"),
    app_commands.Choice(name="Green", value="#57F287"),
    app_commands.Choice(name="Teal", value="#1ABC9C"),
    app_commands.Choice(name="Turquoise", value="#40E0D0"),
    app_commands.Choice(name="Cyan", value="#00FFFF"),
    app_commands.Choice(name="Sky Blue", value="#3498DB"),
    app_commands.Choice(name="Blue", value="#5865F2"),
    app_commands.Choice(name="Navy", value="#2C3E50"),
    app_commands.Choice(name="Indigo", value="#6F2DA8"),
    app_commands.Choice(name="Purple", value="#9B59B6"),
    app_commands.Choice(name="Violet", value="#8F00FF"),
    app_commands.Choice(name="Magenta", value="#FF00FF"),
    app_commands.Choice(name="Pink", value="#EB459E"),
    app_commands.Choice(name="Hot Pink", value="#FF69B4"),
    app_commands.Choice(name="Brown", value="#8B4513"),
    app_commands.Choice(name="Silver", value="#C0C0C0"),
    app_commands.Choice(name="Black", value="#23272A"),
    app_commands.Choice(name="White", value="#FFFFFF"),
]


SUPPORT_PANEL_TITLE = "Discord Support System"


class SupportPanelView(discord.ui.LayoutView):
    """A Components V2 container styled like MetaUpdateView elsewhere in
    the bot — accent-bordered card with a title, rules, category dropdown, and the hub
    banner, instead of the old plain embed + separate View."""

    def __init__(self, *, include_banner: bool = True):
        super().__init__(timeout=None)

        description = (
            "Welcome! Before opening a ticket, please read the rules below "
            "carefully. Our team is here to help with server issues.\n\n"
            "## 📘 Ticket Rules\n"
            "`1.` Please follow our server rules and stay respectful.\n"
            "`2.` Do not open a ticket to report in-game issues.\n"
            "`3.` Do not spam or open multiple tickets for the same issue.\n"
            "`4.` Do not use tickets to report bugs, use the proper bug report channel.\n\n"
            "## ⏳ Response Time\n"
            "If you don't respond within 48 hours, your ticket will be closed.\n\n"
            "## 🤔 Need Help With Something Else?\n"
            "<#1528007337699311740>\n"
            "<#1528009356119900210>\n"
            "<#1528230357072347146>"
        )

        select = discord.ui.Select(
            placeholder="Select a category...",
            options=[
                discord.SelectOption(
                    label="Discord Issue",
                    emoji=discord.PartialEmoji(name="SilverTrophy", id=1528216893297791098),
                ),
                discord.SelectOption(
                    label="Report A Discord User",
                    emoji=discord.PartialEmoji(name="boombox", id=1528218480657170452),
                ),
            ],
            custom_id="support_panel_category_select",
        )
        select.callback = self._make_category_select_callback(select)

        children = [
            discord.ui.TextDisplay(f"# {SUPPORT_PANEL_TITLE}"),
            discord.ui.TextDisplay(description),
            discord.ui.ActionRow(select),
            discord.ui.Separator(),
        ]
        if include_banner:
            children.append(
                discord.ui.MediaGallery(discord.MediaGalleryItem(media=f"attachment://{SUPPORT_BANNER_FILENAME}"))
            )
        children.append(discord.ui.TextDisplay("-# Animal Company: Arena Hub"))

        self.add_item(discord.ui.Container(*children))

    def _make_category_select_callback(self, select: discord.ui.Select):
        async def callback(interaction: discord.Interaction):
            await interaction.response.send_modal(TicketIntakeModal(select.values[0]))

        return callback


# ---------- Persistent "Close" button attached to every ticket thread's first message ----------
class TicketThreadView(discord.ui.LayoutView):
    """A Components V2 container styled like MetaUpdateView elsewhere in
    the bot — replaces the old Embed + separate TicketCloseView with one native container
    holding the ticket details and the Close button together.

    Pass number/category_label/answers/opener to build a real ticket's contents; call with
    no arguments to get a placeholder container used only to re-register the persistent
    Close button's custom_id after a restart (see on_ready)."""

    def __init__(
        self,
        number: int | None = None,
        category_label: str | None = None,
        answers: list[tuple[str, str]] | None = None,
        opener: discord.abc.User | None = None,
    ):
        super().__init__(timeout=None)

        button = discord.ui.Button(
            label="Close", emoji="🔒", style=discord.ButtonStyle.danger, custom_id="ticket_close_button"
        )
        button.callback = self._make_close_callback(button)

        children = []
        if number is not None:
            body = f"**Category:** {category_label}\n\n"
            body += "\n\n".join(f"**{question}**\n{response}" for question, response in answers)
            # Components V2 messages can't carry a top-level `content` field, so the
            # role/opener ping has to live inside the container as its own text
            # component — Discord still parses and notifies on mentions found there.
            children.append(discord.ui.TextDisplay(f"<@&{TICKET_PING_ROLE_ID}> {opener.mention}"))
            children.append(discord.ui.TextDisplay(f"### Ticket #{number}\n{body}"))
            children.append(discord.ui.Separator())
        children.append(discord.ui.ActionRow(button))
        if opener is not None:
            children.append(discord.ui.TextDisplay(f"-# Opened by {opener}"))

        self.add_item(discord.ui.Container(*children))

    def _make_close_callback(self, button: discord.ui.Button):
        async def callback(interaction: discord.Interaction):
            thread = interaction.channel
            if not isinstance(thread, discord.Thread):
                await interaction.response.send_message(
                    "This can only be used inside a ticket thread.", ephemeral=True
                )
                return

            db = load_ticket_db()
            ticket = db["tickets"].get(str(thread.id))

            is_opener = ticket is not None and interaction.user.id == ticket.get("opener_id")
            has_close_role = any(role.id == TICKET_CLOSE_ROLE_ID for role in interaction.user.roles)
            if not (has_staff_role(interaction.user) or has_close_role or is_opener):
                await interaction.response.send_message(
                    "You don't have permission to close this ticket.", ephemeral=True
                )
                return

            await interaction.response.defer()

            if ticket is not None:
                ticket["closed"] = True
                save_ticket_db(db)
                await backup_ticket_db_to_log_channel()

            button.disabled = True
            try:
                await interaction.message.edit(view=self)
            except discord.HTTPException:
                pass

            try:
                await thread.send(f"🔒 Ticket closed by {interaction.user.mention}.")
            except discord.HTTPException:
                pass

            try:
                await thread.edit(
                    name="closed-ticket",
                    archived=True,
                    locked=True,
                    reason=f"Ticket closed by {interaction.user}",
                )
            except discord.HTTPException:
                pass

        return callback


# ---------- Dropdown shown under the support ticket panel banner — opens a ticket thread ----------
async def _create_ticket_thread(
    interaction: discord.Interaction, category_label: str, answers: list[tuple[str, str]]
):
    """Shared by the intake modal: opens the ticket thread, pings the role + opener, posts
    a container with the submitted answers, and logs the ticket to the database. `answers`
    is a list of (question, response) pairs shown in the ticket container."""
    channel = interaction.guild.get_channel(SUPPORT_TICKET_CHANNEL_ID) or await bot.fetch_channel(
        SUPPORT_TICKET_CHANNEL_ID
    )

    db = load_ticket_db()
    number = db["next_number"]
    db["next_number"] = number + 1

    thread_name = f"📈┃{number}-ticket"
    try:
        thread = await channel.create_thread(
            name=thread_name,
            type=discord.ChannelType.private_thread,
            reason=f"Ticket opened by {interaction.user}",
        )
    except discord.HTTPException:
        # Private threads require a certain server boost level; fall back to public
        thread = await channel.create_thread(
            name=thread_name,
            type=discord.ChannelType.public_thread,
            reason=f"Ticket opened by {interaction.user} (private threads unavailable)",
        )

    try:
        await thread.add_user(interaction.user)
    except discord.HTTPException:
        pass

    db["tickets"][str(thread.id)] = {
        "number": number,
        "opener_id": interaction.user.id,
        "category": category_label,
        "answers": {question: response for question, response in answers},
        "closed": False,
        "created_at": discord.utils.utcnow().isoformat(),
    }
    save_ticket_db(db)
    await backup_ticket_db_to_log_channel()

    await thread.send(view=TicketThreadView(number, category_label, answers, interaction.user))

    await interaction.followup.send(f"Ticket created: {thread.mention}", ephemeral=True)


class TicketIntakeModal(discord.ui.Modal, title="Open a Ticket"):
    """Mirrors the intake questions shown on the reference ticket panel. Discord modals
    cap out at 5 components, which is exactly how many fields the reference panel has."""

    about_user = discord.ui.TextInput(
        label="Is your issue about another Discord user?",
        placeholder="Yes or No",
        max_length=10,
    )
    issue = discord.ui.TextInput(
        label="What is the issue you are experiencing?",
        style=discord.TextStyle.paragraph,
        placeholder="Add as much detail as you can!",
        max_length=1000,
    )
    proof = discord.ui.TextInput(
        label="Do you have proof?",
        placeholder="Yes or No",
        max_length=10,
    )
    happened_here = discord.ui.TextInput(
        label="Did the issue happen in this Discord server?",
        placeholder="We can only moderate situations that happen in this Discord server",
        max_length=10,
    )
    no_game_reports = discord.ui.TextInput(
        label="Said no above? Don't file a ticket here!",
        placeholder="We do not handle any game reports here!!!!",
        required=False,
        max_length=100,
    )

    def __init__(self, category_label: str):
        super().__init__()
        self.category_label = category_label

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        if self.happened_here.value.strip().lower().startswith("n"):
            await interaction.followup.send(
                "We can only moderate situations that happen in this Discord server — "
                "this issue can't be filed as a ticket here.",
                ephemeral=True,
            )
            return

        answers = [
            (str(self.about_user.label), self.about_user.value),
            (str(self.issue.label), self.issue.value),
            (str(self.proof.label), self.proof.value),
            (str(self.happened_here.label), self.happened_here.value),
        ]
        await _create_ticket_thread(interaction, self.category_label, answers)


async def refresh_support_ticket_panel():
    """Posts the support ticket panel if one isn't already up in the target channel.
    Unlike before, this does NOT delete and repost the panel on every startup — that would
    spam the channel. It only posts a fresh panel the first time (or if the old one was
    deleted).

    "Already posted" is detected by remembering the panel message's ID in TICKETS_DB_FILE
    (persisted the same way ticket records are, backed up to TICKET_LOG_CHANNEL_ID) and
    trying to fetch that exact message on startup. This used to rely on pinning the panel
    and checking the channel's pins instead — but if pinning itself ever failed (permissions,
    or the channel already at Discord's 50-pin cap), the message would never show up as
    pinned, so every single restart would silently repost another copy. Remembering the ID
    directly sidesteps that failure mode entirely; pinning below is now just a convenience
    for admins, not the detection mechanism."""
    channel = bot.get_channel(SUPPORT_TICKET_CHANNEL_ID) or await bot.fetch_channel(SUPPORT_TICKET_CHANNEL_ID)
    include_banner = os.path.exists(SUPPORT_BANNER_PATH)

    db = load_ticket_db()
    existing_id = db.get("panel_message_id")
    if existing_id is not None:
        try:
            await channel.fetch_message(existing_id)
            return  # panel already posted — leave it alone
        except discord.NotFound:
            pass  # was deleted — fall through and repost
        except discord.HTTPException as e:
            print(f"Couldn't verify the existing support panel message ({e}) — leaving it as-is rather than risk a duplicate.")
            return

    view = SupportPanelView(include_banner=include_banner)

    if not include_banner:
        print(f"Support banner image missing at {SUPPORT_BANNER_PATH} — panel sent without image.")
        sent = await channel.send(view=view)
    else:
        file = discord.File(SUPPORT_BANNER_PATH, filename=SUPPORT_BANNER_FILENAME)
        sent = await channel.send(view=view, file=file)

    db["panel_message_id"] = sent.id
    save_ticket_db(db)
    await backup_ticket_db_to_log_channel()

    try:
        await sent.pin(reason="Support ticket panel")
    except discord.HTTPException as e:
        print(f"Panel posted fine, but couldn't pin it (non-critical — detection no longer depends on this): {e}")


# ---------- Data privacy panel (Toggle Activity Tracking / Delete My Data) ----------
class DataDeleteApprovalView(discord.ui.View):
    """Sent to DATA_DELETE_REQUEST_CHANNEL_ID once a member confirms they want their data
    deleted. Staff approve or deny here. Same non-restart-persistent trade-off as
    ConfirmTeamView above (an in-flight request is lost if the bot restarts before staff
    action it) — acceptable for the same reason: it's rare, and the requester can just
    click Delete My Data again."""

    def __init__(self, requester_id: int):
        super().__init__(timeout=None)
        self.requester_id = requester_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not has_staff_role(interaction.user):
            await interaction.response.send_message(
                "Only staff can action data-deletion requests.", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="Yes, delete", style=discord.ButtonStyle.danger, custom_id="data_delete_approve")
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        for child in self.children:
            child.disabled = True
        try:
            await interaction.message.edit(view=self)
        except discord.HTTPException:
            pass

        summary = await purge_user_activity_data(self.requester_id)

        db = load_optout_db()
        if self.requester_id not in db["deleted_users"]:
            db["deleted_users"].append(self.requester_id)
        save_optout_db(db)
        await backup_optout_db_to_log_channel()

        pending_data_delete_requests.discard(self.requester_id)

        await log_bot_event(
            title="🗑️ User data deleted",
            description=f"<@{self.requester_id}>'s (`{self.requester_id}`) activity data was deleted, "
                        f"approved by {interaction.user.mention}.",
            colour=discord.Colour.red(),
            fields=[("Removed", summary, False)],
        )

        try:
            user = await bot.fetch_user(self.requester_id)
            await user.send(
                f"✅ Your data-deletion request was approved — your activity data has been "
                f"permanently deleted from {BOT_DISPLAY_NAME}."
            )
        except discord.HTTPException:
            pass

        await interaction.followup.send(f"🗑️ Deleted <@{self.requester_id}>'s data.\n{summary}")

    @discord.ui.button(label="No", style=discord.ButtonStyle.secondary, custom_id="data_delete_deny")
    async def deny(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        for child in self.children:
            child.disabled = True
        try:
            await interaction.message.edit(view=self)
        except discord.HTTPException:
            pass

        pending_data_delete_requests.discard(self.requester_id)

        try:
            user = await bot.fetch_user(self.requester_id)
            await user.send(
                f"Your data-deletion request in {BOT_DISPLAY_NAME} was denied by staff. "
                f"Reach out to staff if you'd like to know why."
            )
        except discord.HTTPException:
            pass

        await interaction.followup.send(f"Denied the data-deletion request from <@{self.requester_id}>.")


class DataDeleteConfirmView(discord.ui.View):
    """Ephemeral "are you sure?" shown to the member before their request is forwarded to
    staff — mirrors ConfirmDeleteTeamView's Yes/Cancel pattern elsewhere in the bot."""

    def __init__(self, requester_id: int):
        super().__init__(timeout=120)
        self.requester_id = requester_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message("This prompt isn't for you.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Yes, delete my data", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.requester_id in pending_data_delete_requests:
            for child in self.children:
                child.disabled = True
            await interaction.response.edit_message(
                content="You already have a data-deletion request awaiting staff review.", view=self
            )
            return

        await interaction.response.defer()
        pending_data_delete_requests.add(self.requester_id)

        for child in self.children:
            child.disabled = True
        await safe_edit_original_response(
            interaction,
            content="🗑️ Your data-deletion request has been sent to staff for review.",
            view=self,
        )

        try:
            channel = bot.get_channel(DATA_DELETE_REQUEST_CHANNEL_ID) or await bot.fetch_channel(
                DATA_DELETE_REQUEST_CHANNEL_ID
            )
            embed = discord.Embed(
                title="🗑️ Data-deletion request",
                description=f"{interaction.user.mention} (`{interaction.user.id}`) has requested that "
                            f"all of their tracked activity data be permanently deleted.",
                colour=discord.Colour.orange(),
                timestamp=discord.utils.utcnow(),
            )
            await channel.send(embed=embed, view=DataDeleteApprovalView(interaction.user.id))
        except discord.HTTPException as e:
            pending_data_delete_requests.discard(self.requester_id)
            print(f"Failed to send data-deletion request to staff: {e}")

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(content="Cancelled — nothing was deleted.", view=self)


class PrivacyPanelView(discord.ui.LayoutView):
    """Posted/refreshed in PRIVACY_PANEL_CHANNEL_ID — two Components V2 cards mirroring the
    reference privacy panel: one to toggle activity-tracking opt-out, one to request full
    data deletion. Persistent (custom_id-based, no per-instance state needed in the
    callbacks), so it keeps working across restarts once re-registered in on_ready."""

    def __init__(self):
        super().__init__(timeout=None)

        toggle_button = discord.ui.Button(
            label="Toggle Activity Tracking",
            emoji="📊",
            style=discord.ButtonStyle.secondary,
            custom_id="privacy_toggle_tracking_button",
        )
        toggle_button.callback = self._toggle_tracking_callback

        delete_button = discord.ui.Button(
            label="Delete My Data",
            emoji="🗑️",
            style=discord.ButtonStyle.danger,
            custom_id="privacy_delete_data_button",
        )
        delete_button.callback = self._delete_data_callback

        toggle_text = (
            f"## 📊 Activity Tracking Opt-Out\n"
            f"{BOT_DISPLAY_NAME} reads message text to count activity toward streaks, "
            f"the leaderboard, and message contests.\n\n"
            f"Click below to opt out — your messages will stop counting toward those "
            f"features. This is reversible; click again anytime to opt back in.\n\n"
            f"**This does not affect:** moderation/keyword-safety scanning, which applies "
            f"to all messages in the server regardless of this setting, the same as any "
            f"server-wide word filter.\n\n"
            f"Your choice here is separate from full data deletion — use the Delete My Data "
            f"panel below if you want your existing history removed instead."
        )
        delete_text = (
            f"## 🗑️ Delete My Data\n"
            f"You can request permanent deletion of the activity data {BOT_DISPLAY_NAME} "
            f"has stored about you.\n\n"
            f"**This includes:** message/activity counts and invite-tracker records.\n\n"
            f"**This does NOT include:** messages you've already sent in the server, team "
            f"membership, tickets, or roles you currently hold — those are untouched.\n\n"
            f"⚠️ **This is permanent.** Once approved by staff, we keep a brief note that a "
            f"deletion occurred (not the data itself) so we can confirm it to you if you ask "
            f"again later.\n\n"
            f"Click below to start — a staff member will need to approve the request before "
            f"anything is deleted."
        )

        self.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay(toggle_text),
                discord.ui.ActionRow(toggle_button),
            )
        )
        self.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay(delete_text),
                discord.ui.ActionRow(delete_button),
            )
        )

    async def _toggle_tracking_callback(self, interaction: discord.Interaction):
        db = load_optout_db()
        opted_out = set(db["opted_out"])

        if interaction.user.id in opted_out:
            opted_out.discard(interaction.user.id)
            now_tracked = True
        else:
            opted_out.add(interaction.user.id)
            now_tracked = False

        db["opted_out"] = list(opted_out)
        save_optout_db(db)
        _sync_optout_cache(db)
        await backup_optout_db_to_log_channel()

        if now_tracked:
            message = "✅ You're opted back **in** — your messages will count toward activity tracking again."
        else:
            message = "🚫 You're opted **out** — your messages will no longer count toward activity tracking."
        await interaction.response.send_message(message, ephemeral=True)

    async def _delete_data_callback(self, interaction: discord.Interaction):
        if interaction.user.id in pending_data_delete_requests:
            await interaction.response.send_message(
                "You already have a data-deletion request awaiting staff review.", ephemeral=True
            )
            return
        await interaction.response.send_message(
            "Are you sure? This will permanently delete your tracked activity data once staff approve it.",
            view=DataDeleteConfirmView(interaction.user.id),
            ephemeral=True,
        )


async def refresh_privacy_panel():
    """Posts the privacy panel if one isn't already up in PRIVACY_PANEL_CHANNEL_ID — same
    "post once, remember the message ID, don't repost on every restart" approach as
    refresh_support_ticket_panel() above."""
    channel = bot.get_channel(PRIVACY_PANEL_CHANNEL_ID) or await bot.fetch_channel(PRIVACY_PANEL_CHANNEL_ID)

    db = load_optout_db()
    existing_id = db.get("panel_message_id")
    if existing_id is not None:
        try:
            await channel.fetch_message(existing_id)
            return  # panel already posted — leave it alone
        except discord.NotFound:
            pass  # was deleted — fall through and repost
        except discord.HTTPException as e:
            print(f"Couldn't verify the existing privacy panel message ({e}) — leaving it as-is rather than risk a duplicate.")
            return

    sent = await channel.send(view=PrivacyPanelView())
    db["panel_message_id"] = sent.id
    save_optout_db(db)
    await backup_optout_db_to_log_channel()

    try:
        await channel.edit(
            topic=f"{BOT_DISPLAY_NAME} — manage your activity-tracking preferences and request data deletion here.",
            reason="Privacy panel posted",
        )
    except discord.HTTPException as e:
        print(f"Privacy panel posted fine, but couldn't set the channel topic (non-critical): {e}")


# ---------- Tournament sticky sign-up message ----------
TOURNAMENT_PANEL_TITLE = "Tournament Admin"
TOURNAMENT_SUB_EMOJI = "<:Revolver:1528216974973210747>"

# In-memory cache of every team's channel_id, kept in sync on team create/delete so
# on_message can cheaply check "is this a team channel?" without a load_db() call on
# every single message sent anywhere in the server.
_team_channel_ids: set = set()

# In-memory per-channel cooldown so a burst of chat doesn't cause the sticky sign-up
# message to be deleted and reposted on every single message — not persisted, resets
# on restart, which is harmless (worst case: one extra repost right after a restart).
_tournament_sticky_last_repost: dict = {}


def build_tournament_signup_content(signups: list) -> str:
    """Builds the sticky Tournament Sign-Ups message body for a team channel.
    `signups` is a list of user IDs, capped at TOURNAMENT_SIGNUP_CAP. Only the count is
    shown — signed-up players are tracked in the DB, not listed in the message."""
    coin = "<:CompanyCoins:1528218837030535394>"
    return (
        f"{coin} **Tournament Sign-Ups** {coin}\n"
        "Click the green button below, if you would like to play for the tournament!\n"
        f"**Signed up: `{len(signups)}/{TOURNAMENT_SIGNUP_CAP}`**"
    )


TOURNAMENT_TEAM_SELECT_PAGES = 2  # number of team-select dropdowns on the admin panel (25 teams each)


def build_tournament_team_select_pages(teams: dict) -> list[list[discord.SelectOption]]:
    """Splits every team into pages of up to 25 SelectOptions each (Discord's per-select
    limit), sorted alphabetically. Always returns exactly TOURNAMENT_TEAM_SELECT_PAGES
    pages (padding with an unselectable placeholder if there aren't enough teams to fill
    them) so every dropdown's custom_id is always present on the panel — that keeps them
    working across restarts even if the team count crosses a page boundary later. If you
    ever have more teams than TOURNAMENT_TEAM_SELECT_PAGES * 25, add another page below."""
    all_options = [
        discord.SelectOption(label=f"{info.get('emoji', '')} {name}".strip()[:100], value=name)
        for name, info in sorted(teams.items(), key=lambda kv: kv[0].lower())
    ]
    page_size = 25
    pages = [all_options[i : i + page_size] for i in range(0, len(all_options), page_size)] or [[]]
    while len(pages) < TOURNAMENT_TEAM_SELECT_PAGES:
        pages.append([])
    for page in pages:
        if not page:
            page.append(discord.SelectOption(label="— no teams on this page —", value="__none__"))
    return pages[:TOURNAMENT_TEAM_SELECT_PAGES]


class TournamentSignupView(discord.ui.View):
    """Attached to the sticky sign-up message kept at the bottom of every team channel.
    One button toggles the clicking user on/off their team's sign-up list (capped at
    TOURNAMENT_SIGNUP_CAP) and grants/revokes TOURNAMENT_SUBMISSION_ROLE_ID to match.
    State lives in the teams DB (not the message content), since the sticky message
    itself gets deleted and reposted every time new chat buries it."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Join Tournament", style=discord.ButtonStyle.success, custom_id="tournament_signup_toggle"
    )
    async def toggle(self, interaction: discord.Interaction, button: discord.ui.Button):
        db = load_db()
        team_key = find_team_by_channel(db["teams"], interaction.channel_id)
        if not team_key:
            await interaction.response.send_message(
                "Couldn't figure out which team this sign-up sheet belongs to.", ephemeral=True
            )
            return

        info = db["teams"][team_key]
        signups = info.setdefault("tournament_signups", [])
        user_id = interaction.user.id
        role = interaction.guild.get_role(TOURNAMENT_SUBMISSION_ROLE_ID)

        if user_id in signups:
            signups.remove(user_id)
            save_db(db)
            await backup_db_to_log_channel()
            if role:
                try:
                    await interaction.user.remove_roles(role, reason="Left tournament sign-up")
                except discord.HTTPException:
                    pass
            await interaction.response.edit_message(content=build_tournament_signup_content(signups))
            await interaction.followup.send("You've been removed from the sign-up list.", ephemeral=True)
            return

        if len(signups) >= TOURNAMENT_SIGNUP_CAP:
            await interaction.response.send_message(
                f"Sign-ups are full (`{TOURNAMENT_SIGNUP_CAP}/{TOURNAMENT_SIGNUP_CAP}`).", ephemeral=True
            )
            return

        signups.append(user_id)
        save_db(db)
        await backup_db_to_log_channel()
        if role:
            try:
                await interaction.user.add_roles(role, reason="Signed up for tournament")
            except discord.HTTPException:
                pass
        await interaction.response.edit_message(content=build_tournament_signup_content(signups))
        await interaction.followup.send("You're signed up for the tournament! 🏆", ephemeral=True)


async def post_tournament_sticky(guild: discord.Guild, team_key: str, info: dict, db: dict) -> None:
    """Deletes a team's old sticky sign-up message (if any) and posts a fresh one at the
    bottom of that team's channel, then remembers the new message ID in the DB (and saves
    it). Safe to call repeatedly — e.g. once per new chat message to keep it "stuck" at
    the bottom, once on team creation to seed it, and once per team when staff hit Clear."""
    channel = guild.get_channel(info.get("channel_id"))
    if channel is None:
        return

    old_id = info.get("tournament_message_id")
    if old_id:
        try:
            old_msg = await channel.fetch_message(old_id)
            await old_msg.delete()
        except discord.HTTPException:
            pass

    signups = info.get("tournament_signups", [])
    try:
        new_msg = await channel.send(
            content=build_tournament_signup_content(signups), view=TournamentSignupView()
        )
    except discord.HTTPException as e:
        print(f"[ERROR] Failed to post tournament sticky for {team_key}: {e}")
        return

    info["tournament_message_id"] = new_msg.id
    save_db(db)


async def maybe_restick_tournament_message(message: discord.Message) -> None:
    """Called from on_message for every non-bot message. If the message landed in a team
    channel that has a tournament sticky, and the debounce window has passed, re-sticks it
    (deletes the old one, reposts fresh at the bottom) so it stays visible under new chat."""
    if message.channel.id not in _team_channel_ids:
        return

    now = time.monotonic()
    last = _tournament_sticky_last_repost.get(message.channel.id, 0)
    if now - last < TOURNAMENT_STICKY_DEBOUNCE_SECONDS:
        return

    db = load_db()
    team_key = find_team_by_channel(db["teams"], message.channel.id)
    if not team_key:
        return
    info = db["teams"][team_key]
    if not info.get("tournament_message_id"):
        return  # this team doesn't have a sticky yet (shouldn't normally happen)

    if len(info.get("tournament_signups", [])) >= TOURNAMENT_SIGNUP_CAP:
        return  # full — leave it wherever it is instead of re-sticking; resumes once a spot opens up

    _tournament_sticky_last_repost[message.channel.id] = now
    try:
        await post_tournament_sticky(message.guild, team_key, info, db)
    except Exception as e:
        print(f"[ERROR] Failed to re-stick tournament sign-up message for {team_key}: {e}")


async def ensure_tournament_stickies() -> None:
    """Startup housekeeping: just caches every team's channel_id in _team_channel_ids so
    on_message can cheaply recognize team channels. Sign-up messages are no longer
    auto-posted to every team — staff pick a specific team from the dropdown on the
    tournament admin panel instead (see TournamentAdminPanelView)."""
    db = load_db()
    for info in db["teams"].values():
        _team_channel_ids.add(info.get("channel_id"))


class TournamentAdminPanelView(discord.ui.View):
    """Staff-only utility panel for the tournament:
    - Two team-select dropdowns (each covering up to 25 teams, so 50+ teams are covered
      between the two) that post/refresh the sticky "Join Tournament" sign-up message in
      ONE chosen team's channel at a time — sign-ups are no longer auto-sent to every team.
    - A Clear button that strips TOURNAMENT_SUBMISSION_ROLE_ID from everyone holding it,
      resets every team's sign-up list in the DB, and purges
      TOURNAMENT_CLEAR_PURGE_CHANNEL_ID — ready for the next tournament cycle. It does NOT
      touch any team's sign-up sticky message; those are only posted/refreshed via the
      dropdowns above, or removed via /deletetournamentsignups."""

    def __init__(self, teams: dict | None = None):
        super().__init__(timeout=None)
        if teams is None:
            teams = load_db()["teams"]

        pages = build_tournament_team_select_pages(teams)
        for idx, page_options in enumerate(pages):
            placeholder = (
                "Select a team to send the sign-up to..."
                if len(pages) == 1
                else f"Select a team — dropdown {idx + 1} of {len(pages)}..."
            )
            select = discord.ui.Select(
                placeholder=placeholder,
                custom_id=f"tournament_team_select_{idx + 1}",
                options=page_options,
                min_values=1,
                max_values=1,
            )
            select.callback = self._make_team_select_callback(select)
            self.add_item(select)

    def _make_team_select_callback(self, select: discord.ui.Select):
        async def callback(interaction: discord.Interaction):
            await self._handle_team_select(interaction, select.values[0] if select.values else "__none__")

        return callback

    async def _handle_team_select(self, interaction: discord.Interaction, team_name: str):
        if not has_staff_role(interaction.user):
            await interaction.response.send_message("You don't have permission to use this.", ephemeral=True)
            return

        if team_name == "__none__":
            await interaction.response.send_message("There's nothing to select there.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        db = load_db()
        info = db["teams"].get(team_name)
        if info is None:
            await interaction.followup.send(
                f"**{team_name}** no longer exists — this panel may be out of date. "
                f"Try again in a moment or ask staff to re-run the panel refresh.",
                ephemeral=True,
            )
            return

        try:
            await post_tournament_sticky(interaction.guild, team_name, info, db)
        except Exception as e:
            print(f"[ERROR] Failed to post tournament sign-up for {team_name}: {e}")
            await interaction.followup.send(
                f"❌ Couldn't post the sign-up message for **{team_name}**.", ephemeral=True
            )
            return

        _team_channel_ids.add(info.get("channel_id"))
        await interaction.followup.send(
            f"✅ Posted the tournament sign-up message in **{team_name}**'s channel.", ephemeral=True
        )

    @discord.ui.button(label="Clear", style=discord.ButtonStyle.danger, custom_id="tournament_clear_button")
    async def clear_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not has_staff_role(interaction.user):
            await interaction.response.send_message(
                "You don't have permission to use this.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)

        guild = interaction.guild
        role = guild.get_role(TOURNAMENT_SUBMISSION_ROLE_ID)

        removed = 0
        if role is not None:
            for member in list(role.members):
                try:
                    await member.remove_roles(role, reason=f"Tournament cleared by {interaction.user}")
                    removed += 1
                except discord.HTTPException:
                    pass

        db = load_db()
        for info in db["teams"].values():
            info["tournament_signups"] = []
        save_db(db)
        await backup_db_to_log_channel()

        purge_channel = guild.get_channel(TOURNAMENT_CLEAR_PURGE_CHANNEL_ID) or bot.get_channel(
            TOURNAMENT_CLEAR_PURGE_CHANNEL_ID
        )
        if purge_channel is None:
            try:
                purge_channel = await bot.fetch_channel(TOURNAMENT_CLEAR_PURGE_CHANNEL_ID)
            except discord.HTTPException:
                purge_channel = None

        purged = 0
        if purge_channel is not None:
            try:
                deleted = await purge_channel.purge(limit=None)
                purged = len(deleted)
            except discord.HTTPException:
                pass

        channel_mention = purge_channel.mention if purge_channel else "the target channel"
        role_note = "the tournament role" if role is not None else "the tournament role (role not found!)"
        await interaction.followup.send(
            f"🧹 Cleared — removed {role_note} from {removed} member(s), reset every team's "
            f"sign-up list, and purged {purged} message(s) from {channel_mention}.",
            ephemeral=True,
        )


async def repost_tournament_panel() -> None:
    """Deletes the existing tournament admin panel (if any) and posts a fresh one with the
    team dropdown rebuilt from the current DB. Call this whenever the team list changes
    (create/delete/rename) so the dropdown doesn't go stale. Do NOT call this on every
    startup/redeploy — use post_tournament_panel_if_missing() there instead, so the panel
    isn't deleted and reposted (and doesn't lose its position in the channel) on every
    restart."""
    channel = bot.get_channel(TOURNAMENT_PANEL_CHANNEL_ID) or await bot.fetch_channel(TOURNAMENT_PANEL_CHANNEL_ID)

    async for msg in channel.history(limit=50):
        if msg.author.id == bot.user.id and msg.embeds and msg.embeds[0].title == TOURNAMENT_PANEL_TITLE:
            try:
                await msg.delete()
            except discord.HTTPException:
                pass
            break

    db = load_db()
    await channel.send(embed=_build_tournament_panel_embed(), view=TournamentAdminPanelView(db["teams"]))


async def post_tournament_panel_if_missing() -> None:
    """Posts the tournament admin panel only if one isn't already up in the target channel
    (e.g. first boot, or the message was deleted). Called on every startup/redeploy instead
    of repost_tournament_panel() — it deliberately leaves an existing panel (and its
    dropdown) untouched so nothing gets deleted/resent just because the bot restarted."""
    channel = bot.get_channel(TOURNAMENT_PANEL_CHANNEL_ID) or await bot.fetch_channel(TOURNAMENT_PANEL_CHANNEL_ID)

    async for msg in channel.history(limit=50):
        if msg.author.id == bot.user.id and msg.embeds and msg.embeds[0].title == TOURNAMENT_PANEL_TITLE:
            return  # panel already posted — leave it alone

    db = load_db()
    await channel.send(embed=_build_tournament_panel_embed(), view=TournamentAdminPanelView(db["teams"]))


def _build_tournament_panel_embed() -> discord.Embed:
    return discord.Embed(
        title=TOURNAMENT_PANEL_TITLE,
        description=(
            "Pick a team from the dropdown to post (or refresh) the **Join Tournament** "
            "sign-up message in that team's channel.\n\n"
            "**Clear** removes the tournament role from everyone, resets every team's "
            "sign-up list, and purges the announcement channel — staff only. It doesn't "
            "touch any posted sign-up messages."
        ),
        colour=discord.Colour.gold(),
    )


async def perform_team_kick(db: dict, team_name: str, user_id: int, guild: discord.Guild, reason: str) -> bool:
    """Removes a single member's team role and DB membership (does not touch the leader).
    Returns False if the team or membership doesn't exist. Note this never enforces the
    TEAM_JOIN_COOLDOWN_DAYS wait — that cooldown only applies to a member voluntarily using
    /leaveteam, not to being kicked."""
    info = db["teams"].get(team_name)
    if info is None or user_id not in info.get("members", []):
        return False

    member = guild.get_member(user_id)
    role = guild.get_role(info["role_id"])
    if role and member:
        try:
            await member.remove_roles(role, reason=reason)
        except discord.HTTPException:
            pass

    # If the person being kicked is the team's co-leader, clear that slot and revert their
    # elevated channel permissions — they're leaving the team entirely, not just stepping
    # down from co-leader, so this needs to happen regardless of who initiated the kick.
    if info.get("co_leader_id") == user_id:
        info["co_leader_id"] = None
        channel_for_perms = guild.get_channel(info.get("channel_id"))
        if channel_for_perms is not None and member is not None:
            try:
                await channel_for_perms.set_permissions(member, overwrite=None, reason=reason)
            except discord.HTTPException:
                pass
        if member is not None:
            co_leader_role = guild.get_role(TEAM_CO_LEADER_ROLE_ID)
            if co_leader_role is not None and co_leader_role in member.roles:
                try:
                    await member.remove_roles(co_leader_role, reason=reason)
                except discord.HTTPException:
                    pass

    info["members"] = [uid for uid in info["members"] if uid != user_id]
    clear_team_join(info, user_id)
    save_db(db)
    await backup_db_to_log_channel()

    await log_team_event(
        "👢 Member Removed From Team",
        colour=discord.Colour.red(),
        fields=[
            ("Team", team_name, True),
            ("Member", f"<@{user_id}> (`{user_id}`)", True),
            ("Reason", reason, False),
        ],
    )

    channel = guild.get_channel(info["channel_id"])
    if channel is not None:
        try:
            await channel.send(build_team_leave_message(user_id))
        except discord.HTTPException:
            pass

    return True


async def perform_team_deletion(db: dict, team_name: str, guild: discord.Guild, reason: str) -> bool:
    """Removes a team's role, channel, and DB entry. Returns False if the team was already gone."""
    info = db["teams"].pop(team_name, None)
    if info is None:
        return False

    role = guild.get_role(info["role_id"])
    if role:
        await role.delete(reason=reason)

    channel = guild.get_channel(info["channel_id"])
    if channel:
        await channel.delete(reason=reason)

    _team_channel_ids.discard(info["channel_id"])
    try:
        await repost_tournament_panel()
    except Exception as e:
        print(f"[ERROR] Failed to refresh tournament panel after deleting {team_name}: {e}")

    leader_id = info.get("leader_id")
    if leader_id is not None:
        leader_marker_role = guild.get_role(TEAM_LEADER_ROLE_ID)
        if leader_marker_role is not None:
            try:
                leader_member = guild.get_member(leader_id) or await guild.fetch_member(leader_id)
                if leader_marker_role in leader_member.roles:
                    await leader_member.remove_roles(leader_marker_role, reason=reason)
            except discord.HTTPException:
                pass

    co_leader_id = info.get("co_leader_id")
    if co_leader_id is not None:
        co_leader_role = guild.get_role(TEAM_CO_LEADER_ROLE_ID)
        if co_leader_role is not None:
            try:
                co_leader_member = guild.get_member(co_leader_id) or await guild.fetch_member(co_leader_id)
                if co_leader_role in co_leader_member.roles:
                    await co_leader_member.remove_roles(co_leader_role, reason=reason)
            except discord.HTTPException:
                pass

    save_db(db)
    await backup_db_to_log_channel()

    await log_team_event(
        "🗑️ Team Deleted",
        colour=discord.Colour.dark_red(),
        fields=[
            ("Team", team_name, True),
            ("Leader", f"<@{leader_id}> (`{leader_id}`)" if leader_id is not None else "Unknown", True),
            ("Members", str(len(info.get("members", []))), True),
            ("Reason", reason, False),
        ],
    )
    return True


async def perform_leader_promotion(
    db: dict, team_name: str, new_leader_id: int, guild: discord.Guild, reason: str
) -> bool:
    """Swaps a team's leader to new_leader_id (the caller must already have confirmed
    new_leader_id is a current member of the team). Moves the TEAM_LEADER_ROLE_ID marker
    role and the special channel permission overwrite (manage_messages/mention_everyone)
    from the old leader to the new one, and saves the DB. Returns False if the team no
    longer exists."""
    info = db["teams"].get(team_name)
    if info is None:
        return False

    old_leader_id = info.get("leader_id")
    info["leader_id"] = new_leader_id

    # If the new leader was the co-leader, that slot is meaningless now (can't be both) —
    # clear it and its extra channel perms/role before the leader-promotion perms below
    # apply (which grant the same/more anyway).
    if info.get("co_leader_id") == new_leader_id:
        await _clear_team_coleader(db, team_name, guild, reason=reason)

    channel = guild.get_channel(info.get("channel_id"))
    leader_marker_role = guild.get_role(TEAM_LEADER_ROLE_ID)

    try:
        new_member = guild.get_member(new_leader_id) or await guild.fetch_member(new_leader_id)
    except discord.HTTPException:
        new_member = None

    old_member = guild.get_member(old_leader_id) if old_leader_id is not None else None

    if channel is not None:
        if old_member is not None:
            try:
                # Reverts the old leader back to whatever the team role alone grants them —
                # removes their manage_messages/mention_everyone override, not the channel.
                await channel.set_permissions(old_member, overwrite=None, reason=reason)
            except discord.HTTPException:
                pass
        if new_member is not None:
            try:
                await channel.set_permissions(
                    new_member, overwrite=team_leader_channel_overwrite(), reason=reason
                )
            except discord.HTTPException:
                pass

    if leader_marker_role is not None:
        if new_member is not None and leader_marker_role not in new_member.roles:
            try:
                await new_member.add_roles(leader_marker_role, reason=reason)
            except discord.HTTPException:
                pass

        if old_member is not None and leader_marker_role in old_member.roles:
            # Only strip the marker role if the old leader doesn't lead a different team too.
            still_leads_other = any(
                other_name != team_name and other_info.get("leader_id") == old_leader_id
                for other_name, other_info in db["teams"].items()
            )
            if not still_leads_other:
                try:
                    await old_member.remove_roles(leader_marker_role, reason=reason)
                except discord.HTTPException:
                    pass

    save_db(db)
    await backup_db_to_log_channel()

    await log_team_event(
        "👑 Team Leader Changed",
        colour=discord.Colour.gold(),
        fields=[
            ("Team", team_name, True),
            ("New Leader", f"<@{new_leader_id}> (`{new_leader_id}`)", True),
            (
                "Old Leader",
                f"<@{old_leader_id}> (`{old_leader_id}`)" if old_leader_id is not None else "Unknown",
                True,
            ),
            ("Reason", reason, False),
        ],
    )
    return True


async def sync_existing_teams():
    """Backfill pass run on every startup: makes sure every current team leader holds
    TEAM_LEADER_ROLE_ID and has the manage-messages/mention-everyone overrides in their
    own team channel (so they can ping the team, delete messages, and pin messages), that
    any leftover TEAM_CHANNEL_FULL_ACCESS_ROLE_ID / TEAM_CHANNEL_VIEWER_ROLE_ID overwrites
    are stripped out of every team channel (those roles no longer get team-channel access),
    and that TEAM_CHANNEL_BASIC_ACCESS_ROLE_ID has basic access in every team channel except
    TEAM_CHANNEL_BASIC_ACCESS_EXCLUDED_CHANNEL_ID. Runs against every team every time, so any
    newly created team gets picked up the next time this runs. Idempotent — cheap after the
    first run, and self-heals if a permission or role is ever reverted manually."""
    db = load_db()
    if not db["teams"]:
        return

    guild = None
    leader_role_granted = 0
    perms_updated = 0
    legacy_access_removed = 0
    basic_access_updated = 0

    for team_name, info in db["teams"].items():
        leader_id = info.get("leader_id")
        channel_id = info.get("channel_id")

        if guild is None and channel_id is not None:
            try:
                # all teams live in one guild for this bot; grab it from any known channel
                seed_channel = bot.get_channel(channel_id) or await bot.fetch_channel(channel_id)
                guild = seed_channel.guild
            except discord.HTTPException:
                continue

        if guild is None:
            continue

        channel = guild.get_channel(channel_id) if channel_id is not None else None

        # Strip any leftover overwrites for the two legacy roles, and backfill the new
        # basic-access role's channel permissions, regardless of who leads the team.
        if channel is not None:
            for legacy_role_id in (TEAM_CHANNEL_FULL_ACCESS_ROLE_ID, TEAM_CHANNEL_VIEWER_ROLE_ID):
                legacy_role = guild.get_role(legacy_role_id)
                if legacy_role is not None and legacy_role in channel.overwrites:
                    try:
                        await channel.set_permissions(
                            legacy_role,
                            overwrite=None,
                            reason=f"Removed legacy team-channel access role from {team_name}",
                        )
                        legacy_access_removed += 1
                    except discord.HTTPException:
                        pass

            if channel_id == TEAM_CHANNEL_BASIC_ACCESS_EXCLUDED_CHANNEL_ID:
                basic_role = guild.get_role(TEAM_CHANNEL_BASIC_ACCESS_ROLE_ID)
                if basic_role is not None and basic_role in channel.overwrites:
                    try:
                        await channel.set_permissions(
                            basic_role,
                            overwrite=None,
                            reason=f"Excluded team channel — basic-access role should not have access here ({team_name})",
                        )
                    except discord.HTTPException:
                        pass
            else:
                basic_role = guild.get_role(TEAM_CHANNEL_BASIC_ACCESS_ROLE_ID)
                if basic_role is not None:
                    allow, _deny = channel.overwrites_for(basic_role).pair()
                    if allow != team_channel_basic_access_overwrite().pair()[0]:
                        try:
                            await channel.set_permissions(
                                basic_role,
                                overwrite=team_channel_basic_access_overwrite(),
                                reason=f"Backfilled basic-access role permissions for existing team {team_name}",
                            )
                            basic_access_updated += 1
                        except discord.HTTPException:
                            pass

        if leader_id is None:
            continue

        try:
            member = guild.get_member(leader_id) or await guild.fetch_member(leader_id)
        except discord.HTTPException:
            continue

        leader_marker_role = guild.get_role(TEAM_LEADER_ROLE_ID)
        if leader_marker_role is None:
            print(f"TEAM_LEADER_ROLE_ID ({TEAM_LEADER_ROLE_ID}) not found in guild — skipping role backfill.")
        elif leader_marker_role not in member.roles:
            try:
                await member.add_roles(
                    leader_marker_role, reason=f"Backfilled team-leader role for existing team {team_name}"
                )
                leader_role_granted += 1
            except discord.HTTPException:
                pass

        if channel is not None:
            existing = channel.overwrites_for(member)
            if not (existing.manage_messages and existing.mention_everyone):
                try:
                    await channel.set_permissions(
                        member,
                        overwrite=team_leader_channel_overwrite(),
                        reason=f"Backfilled leader channel permissions for existing team {team_name}",
                    )
                    perms_updated += 1
                except discord.HTTPException:
                    pass

    if leader_role_granted or perms_updated or legacy_access_removed or basic_access_updated:
        print(
            f"Backfilled team-leader role onto {leader_role_granted} leader(s), channel "
            f"permissions onto {perms_updated} leader(s), removed {legacy_access_removed} "
            f"legacy access-role overwrite(s), and backfilled basic-access role permissions "
            f"onto {basic_access_updated} team channel(s)."
        )


# ---------- Delete-existing-team view (shown when a leader tries to make a 2nd team) ----------
class DeleteTeamView(discord.ui.View):
    def __init__(self, author_id: int, team_name: str, guild: discord.Guild):
        super().__init__(timeout=120)
        self.author_id = author_id
        self.team_name = team_name
        self.guild = guild

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("This prompt isn't for you.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Delete current team", style=discord.ButtonStyle.danger)
    async def delete_team(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        db = load_db()
        deleted = await perform_team_deletion(
            db, self.team_name, self.guild, reason=f"Team deleted by {interaction.user}"
        )
        if not deleted:
            await safe_edit_original_response(interaction, content="That team no longer exists.", view=None)
            return

        for child in self.children:
            child.disabled = True
        await safe_edit_original_response(
            interaction,
            content=f"🗑️ Team **{self.team_name}** deleted. You can now create a new one.",
            view=self,
        )


# ---------- Confirmation view for team deletion (used by /changeteamsettings and /staffchangesetting) ----------

class ConfirmDeleteTeamView(discord.ui.View):
    def __init__(self, invoker_id: int, team_name: str, guild: discord.Guild):
        super().__init__(timeout=60)
        self.invoker_id = invoker_id
        self.team_name = team_name
        self.guild = guild

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.invoker_id:
            await interaction.response.send_message("This prompt isn't for you.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Yes, delete it", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        db = load_db()
        deleted = await perform_team_deletion(
            db, self.team_name, self.guild, reason=f"Team deleted by staff member {interaction.user}"
        )
        for child in self.children:
            child.disabled = True
        if not deleted:
            await safe_edit_original_response(interaction, content="That team no longer exists.", view=self)
            return
        await safe_edit_original_response(
            interaction, content=f"🗑️ Team **{self.team_name}** has been deleted.", view=self
        )

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(content="Cancelled — team not deleted.", view=self)


# ---------- Confirmation view for /leaderpromote ----------
class ConfirmLeaderPromoteView(discord.ui.View):
    def __init__(self, invoker_id: int, team_name: str, new_leader_id: int, guild: discord.Guild):
        super().__init__(timeout=60)
        self.invoker_id = invoker_id
        self.team_name = team_name
        self.new_leader_id = new_leader_id
        self.guild = guild

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.invoker_id:
            await interaction.response.send_message("This prompt isn't for you.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Yes, promote them", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()

        db = load_db()
        info = db["teams"].get(self.team_name)
        if info is None or info.get("leader_id") != self.invoker_id:
            for child in self.children:
                child.disabled = True
            await safe_edit_original_response(
                interaction,
                content="That team no longer exists, or you're no longer its leader.",
                view=self,
            )
            return

        promoted = await perform_leader_promotion(
            db, self.team_name, self.new_leader_id, self.guild,
            reason=f"Leader promotion by {interaction.user}",
        )
        for child in self.children:
            child.disabled = True

        if not promoted:
            await safe_edit_original_response(interaction, content="That team no longer exists.", view=self)
            return

        await safe_edit_original_response(
            interaction,
            content=f"✅ <@{self.new_leader_id}> is now the leader of **{self.team_name}**.",
            view=self,
        )

        channel = self.guild.get_channel(info.get("channel_id"))
        if channel is not None:
            try:
                await channel.send(f"👑 <@{self.new_leader_id}> is now the leader of the team!")
            except discord.HTTPException:
                pass

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(content="Cancelled — leadership unchanged.", view=self)


# ---------- Confirmation view for /cleanuporphanteams ----------
class ConfirmCleanupView(discord.ui.View):
    def __init__(self, invoker_id: int, orphans: list):
        super().__init__(timeout=120)
        self.invoker_id = invoker_id
        self.orphans = orphans  # list of (channel, role_or_None)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.invoker_id:
            await interaction.response.send_message("This prompt isn't for you.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Yes, delete them", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        try:
            await interaction.delete_original_response()
        except discord.HTTPException:
            pass

        deleted_channels = 0
        deleted_roles = 0
        for channel, role in self.orphans:
            if role is not None:
                try:
                    await role.delete(reason=f"Orphan team role cleanup by {interaction.user}")
                    deleted_roles += 1
                except discord.HTTPException:
                    pass
            try:
                await channel.delete(reason=f"Orphan team channel cleanup by {interaction.user}")
                deleted_channels += 1
            except discord.HTTPException:
                pass

        await interaction.followup.send(
            f"🧹 Cleanup complete — deleted {deleted_channels} channel(s) and {deleted_roles} role(s).",
            ephemeral=True,
        )

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        try:
            await interaction.delete_original_response()
        except discord.HTTPException:
            pass
        await interaction.followup.send("Cleanup cancelled — nothing was deleted.", ephemeral=True)


# ---------- Confirmation view for /cleanup (solo, leader-only teams) ----------
class ConfirmSoloTeamCleanupView(discord.ui.View):
    def __init__(self, invoker_id: int, team_names: list, guild: discord.Guild):
        super().__init__(timeout=120)
        self.invoker_id = invoker_id
        self.team_names = team_names
        self.guild = guild

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.invoker_id:
            await interaction.response.send_message("This prompt isn't for you.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Yes, delete them", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        try:
            await interaction.delete_original_response()
        except discord.HTTPException:
            pass

        deleted = []
        skipped = []
        db = load_db()
        for team_name in self.team_names:
            # Re-check membership against the latest data in case something changed
            # (e.g. someone joined) between the confirmation prompt and this click.
            info = db["teams"].get(team_name)
            if info is None or len(info.get("members", [])) != 1 or info["members"][0] != info.get("leader_id"):
                skipped.append(team_name)
                continue
            reason = f"Solo (leader-only) team cleanup by {interaction.user}"
            ok = await perform_team_deletion(db, team_name, self.guild, reason=reason)
            if ok:
                deleted.append(team_name)
            else:
                skipped.append(team_name)

        message = f"🧹 Cleanup complete — deleted {len(deleted)} solo team(s)."
        if deleted:
            message += "\n" + ", ".join(f"**{name}**" for name in deleted)
        if skipped:
            message += f"\n⚠️ Skipped {len(skipped)} (no longer solo, or already gone): " + ", ".join(skipped)
        await interaction.followup.send(message, ephemeral=True)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        try:
            await interaction.delete_original_response()
        except discord.HTTPException:
            pass
        await interaction.followup.send("Cleanup cancelled — nothing was deleted.", ephemeral=True)


# ---------- Admin confirmation view for /createteam ----------
class ConfirmTeamView(discord.ui.View):
    def __init__(self, requester_id: int, team_name: str, emoji: str, colour: str, guild: discord.Guild):
        super().__init__(timeout=None)
        self.requester_id = requester_id
        self.team_name = team_name
        self.emoji = emoji
        self.colour = colour
        self.guild = guild
        self.message: discord.Message = None  # set by the caller after sending

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message(
                "Only admins can confirm team creation.", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="Yes", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        try:
            await interaction.delete_original_response()
        except discord.HTTPException:
            pass

        guild = self.guild
        primary_category = guild.get_channel(TEAM_CATEGORY_ID)
        overflow_category = guild.get_channel(TEAM_CATEGORY_OVERFLOW_ID)

        category = primary_category
        if category is None or (category is not None and len(category.channels) >= 50):
            category = overflow_category

        if category is None or len(category.channels) >= 50:
            pending_team_requests.discard(self.requester_id)
            await interaction.followup.send(
                f"❌ Couldn't create **{self.team_name}** — both team categories are full "
                f"(Discord's 50-channel limit). Delete or move some existing team channels "
                f"out of one of the categories, then have the requester try `/createteam` again."
            )
            return

        role_colour = discord.Colour.from_str(self.colour)
        try:
            role = await guild.create_role(
                name=f"{self.team_name} Team",
                colour=role_colour,
                display_icon=self.emoji,
                reason=f"Team created, confirmed by {interaction.user}",
            )
        except discord.HTTPException:
            # Role icons require a certain server boost level; fall back without one
            role = await guild.create_role(
                name=f"{self.team_name} Team",
                colour=role_colour,
                reason=f"Team created, confirmed by {interaction.user} (role icons unavailable)",
            )

        reference_role = guild.get_role(REFERENCE_ROLE_ID)
        if reference_role is not None:
            try:
                await role.edit(
                    position=reference_role.position + 1,
                    reason="Keep team role above reference role",
                )
            except discord.HTTPException:
                # Bot's own top role may be too low to move things this high; skip silently
                pass

        leader = guild.get_member(self.requester_id) or await guild.fetch_member(self.requester_id)

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            role: discord.PermissionOverwrite(view_channel=True, send_messages=True),
            guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True),
            # Leader gets extra rights in their own channel: delete/pin messages, and ping
            # the team role even though it isn't set to be mentionable.
            leader: team_leader_channel_overwrite(),
        }

        basic_access_role = guild.get_role(TEAM_CHANNEL_BASIC_ACCESS_ROLE_ID)
        if basic_access_role is not None:
            overwrites[basic_access_role] = team_channel_basic_access_overwrite()

        channel_name = f"{self.emoji}┃{self.team_name}-Team"
        try:
            team_channel = await guild.create_text_channel(
                name=channel_name,
                category=category,
                overwrites=overwrites,
                reason=f"Team created, confirmed by {interaction.user}",
            )
        except discord.HTTPException as e:
            # Channel creation failed (e.g. the category filled up to Discord's 50-channel
            # cap in the time between our check above and this call). Clean up the role we
            # already created so we don't leave an orphan behind for /cleanuporphanteams to
            # find later, and let the admin know the team was NOT created.
            try:
                await role.delete(reason="Team channel creation failed — cleaning up orphaned role")
            except discord.HTTPException:
                pass
            pending_team_requests.discard(self.requester_id)
            await interaction.followup.send(
                f"❌ Couldn't create the channel for **{self.team_name}** (Discord rejected the "
                f"request — the team category may have just filled up). No role was left "
                f"behind; the requester can safely try `/createteam` again."
            )
            return

        await leader.add_roles(role, reason="New team leader")

        leader_marker_role = guild.get_role(TEAM_LEADER_ROLE_ID)
        if leader_marker_role is not None:
            try:
                await leader.add_roles(leader_marker_role, reason="New team leader")
            except discord.HTTPException:
                pass

        try:
            await leader.send(f"You're now the leader of **{self.team_name}** {self.emoji}!")
        except discord.Forbidden:
            pass

        try:
            await team_channel.send(build_team_welcome_message(self.requester_id, role.id))
        except discord.HTTPException:
            pass

        db = load_db()
        db["teams"][self.team_name] = {
            "emoji": self.emoji,
            "leader_id": self.requester_id,
            "co_leader_id": None,
            "role_id": role.id,
            "channel_id": team_channel.id,
            "members": [self.requester_id],
        }
        record_team_join(db["teams"][self.team_name], self.requester_id)
        save_db(db)
        await backup_db_to_log_channel()
        pending_team_requests.discard(self.requester_id)

        _team_channel_ids.add(team_channel.id)
        try:
            await repost_tournament_panel()
        except Exception as e:
            print(f"[ERROR] Failed to refresh tournament panel for new team {self.team_name}: {e}")

        await log_team_event(
            "🆕 Team Created",
            colour=discord.Colour.green(),
            fields=[
                ("Team", f"{self.emoji} {self.team_name}", True),
                ("Leader", f"{leader.mention} (`{leader.id}`)", True),
                ("Confirmed By", f"{interaction.user.mention} (`{interaction.user.id}`)", True),
                ("Channel", team_channel.mention, True),
                ("Role", role.mention, True),
            ],
        )

        await interaction.followup.send(
            f"✅ Team **{self.team_name}** {self.emoji} created — {team_channel.mention}"
        )

    @discord.ui.button(label="No", style=discord.ButtonStyle.danger)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        try:
            await interaction.delete_original_response()
        except discord.HTTPException:
            pass
        pending_team_requests.discard(self.requester_id)
        await log_team_event(
            "🚫 Team Creation Denied",
            colour=discord.Colour.dark_grey(),
            fields=[
                ("Team", f"{self.emoji} {self.team_name}", True),
                ("Requester", f"<@{self.requester_id}> (`{self.requester_id}`)", True),
                ("Denied By", f"{interaction.user.mention} (`{interaction.user.id}`)", True),
            ],
        )
        await interaction.followup.send("Team creation denied.", ephemeral=True)


# ---------- Cancel-pending-invite helper + view ----------
async def _cancel_pending_invite(leader_id: int, note: str = "This invite was cancelled.") -> dict | None:
    """Clears leader_id's pending-invite record (if any) and, if the DM message can still
    be reached, edits it to `note` and strips the Yes/No buttons so the invited user can no
    longer act on it. Returns the popped pending-invite record, or None if there wasn't one."""
    pending = pending_invites.pop(leader_id, None)
    if pending is None:
        return None

    try:
        dm_channel = bot.get_channel(pending["dm_channel_id"])
        if dm_channel is None:
            dm_channel = await bot.fetch_channel(pending["dm_channel_id"])
        message = await dm_channel.fetch_message(pending["dm_message_id"])
        await message.edit(content=note, view=None)
    except discord.HTTPException:
        pass  # DM/message may already be gone — the record is still cleared either way

    return pending


class CancelInviteView(discord.ui.View):
    """Shown ephemerally to a team leader who already has an invite pending, with a single
    red button that cancels it so they're free to send a new one."""

    def __init__(self, leader_id: int):
        super().__init__(timeout=120)
        self.leader_id = leader_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.leader_id:
            await interaction.response.send_message("This prompt isn't for you.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Cancel Invite", style=discord.ButtonStyle.danger)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)

        pending = await _cancel_pending_invite(
            self.leader_id, note="This invite was cancelled by the team leader."
        )
        for child in self.children:
            child.disabled = True

        if pending is None:
            await safe_edit_original_response(
                interaction,
                content="That invite is no longer pending — you're free to send a new one.",
                view=self,
            )
            return

        await log_team_event(
            "🚫 Invite Cancelled",
            colour=discord.Colour.dark_grey(),
            fields=[
                ("Team", pending.get("team", "Unknown"), True),
                ("Invited User", f"<@{pending['invited_user_id']}> (`{pending['invited_user_id']}`)", True),
                ("Cancelled By", f"<@{self.leader_id}> (`{self.leader_id}`)", True),
            ],
        )

        await safe_edit_original_response(
            interaction,
            content=f"✅ Cancelled the pending invite to <@{pending['invited_user_id']}>. "
            f"You can now invite someone else.",
            view=self,
        )


# ---------- Invite response view (DM'd to the invited user) ----------
class InviteResponseView(discord.ui.View):
    def __init__(self, team_name: str, invited_user_id: int, guild_id: int, inviter_id: int):
        super().__init__(timeout=86400)  # 24h to respond
        self.team_name = team_name
        self.invited_user_id = invited_user_id
        self.guild_id = guild_id
        self.inviter_id = inviter_id
        self.message: discord.Message = None  # set by the caller after sending

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.invited_user_id:
            await interaction.response.send_message("This invite isn't for you.", ephemeral=True)
            return False
        return True

    def _clear_pending(self):
        # Only clear the leader's pending-invite slot if it's still tracking *this* invite —
        # avoids wiping out a newer invite the leader may have sent after this one resolved.
        pending = pending_invites.get(self.inviter_id)
        if pending and pending.get("invited_user_id") == self.invited_user_id and pending.get("team") == self.team_name:
            pending_invites.pop(self.inviter_id, None)

    @discord.ui.button(label="Yes", style=discord.ButtonStyle.success)
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()

        db = load_db()
        info = db["teams"].get(self.team_name)
        if info is None:
            self._clear_pending()
            for child in self.children:
                child.disabled = True
            await safe_edit_original_response(interaction, content="This team no longer exists.", view=self)
            return

        if (
            self.invited_user_id not in info.get("members", [])
            and not info.get("bypass_member_limit", False)
            and len(info.get("members", [])) >= MAX_TEAM_MEMBERS
        ):
            self._clear_pending()
            for child in self.children:
                child.disabled = True
            await safe_edit_original_response(
                interaction,
                content=f"**{self.team_name}** filled up to the {MAX_TEAM_MEMBERS}-member cap "
                f"before you accepted — ask the leader to check again.",
                view=self,
            )
            return

        guild = bot.get_guild(self.guild_id)
        member = guild.get_member(self.invited_user_id) or await guild.fetch_member(self.invited_user_id)
        role = guild.get_role(info["role_id"])
        if role:
            await member.add_roles(role, reason="Accepted team invite")

        if self.invited_user_id not in info["members"]:
            info["members"].append(self.invited_user_id)
        record_team_join(info, self.invited_user_id)
        save_db(db)
        await backup_db_to_log_channel()

        self._clear_pending()

        channel = guild.get_channel(info["channel_id"])
        if channel:
            await channel.send(f"🎉 {member.mention} just joined the team!")

        await log_team_event(
            "✅ Invite Accepted — Member Joined",
            colour=discord.Colour.green(),
            fields=[
                ("Team", self.team_name, True),
                ("New Member", f"{member.mention} (`{member.id}`)", True),
                ("Invited By", f"<@{self.inviter_id}> (`{self.inviter_id}`)", True),
            ],
        )

        for child in self.children:
            child.disabled = True
        await safe_edit_original_response(interaction, content=f"You joined **{self.team_name}**! 🎉", view=self)

    @discord.ui.button(label="No", style=discord.ButtonStyle.danger)
    async def decline(self, interaction: discord.Interaction, button: discord.ui.Button):
        self._clear_pending()
        for child in self.children:
            child.disabled = True
        await log_team_event(
            "❌ Invite Declined",
            colour=discord.Colour.orange(),
            fields=[
                ("Team", self.team_name, True),
                ("Invited User", f"<@{self.invited_user_id}> (`{self.invited_user_id}`)", True),
                ("Invited By", f"<@{self.inviter_id}> (`{self.inviter_id}`)", True),
            ],
        )
        await interaction.response.edit_message(content="Invite declined.", view=self)

    async def on_timeout(self):
        # The invited user never responded within 24h — free up the leader's pending slot
        # and disable the stale buttons, same as an explicit decline.
        self._clear_pending()
        for child in self.children:
            child.disabled = True
        await log_team_event(
            "⌛ Invite Expired",
            colour=discord.Colour.dark_grey(),
            fields=[
                ("Team", self.team_name, True),
                ("Invited User", f"<@{self.invited_user_id}> (`{self.invited_user_id}`)", True),
                ("Invited By", f"<@{self.inviter_id}> (`{self.inviter_id}`)", True),
            ],
        )
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


async def _cancel_pending_scrim_request(leader_id: int, note: str = "This scrim request was cancelled.") -> dict | None:
    """Clears leader_id's pending-scrim-request record (if any) and, if the DM message can
    still be found, disables its buttons and appends note to it. Returns the cleared record,
    or None if there wasn't one."""
    pending = pending_scrim_requests.pop(leader_id, None)
    if pending is None:
        return None
    try:
        channel = bot.get_channel(pending["dm_channel_id"]) or await bot.fetch_channel(pending["dm_channel_id"])
        msg = await channel.fetch_message(pending["dm_message_id"])
        view = discord.ui.View.from_message(msg)
        for child in view.children:
            child.disabled = True
        await msg.edit(content=f"{msg.content}\n\n_{note}_", view=view)
    except discord.HTTPException:
        pass
    return pending


class ScrimResponseView(discord.ui.View):
    def __init__(self, challenger_team: str, challenger_leader_id: int, opponent_team: str, opponent_leader_id: int, guild_id: int):
        super().__init__(timeout=86400)  # 24h to respond
        self.challenger_team = challenger_team
        self.challenger_leader_id = challenger_leader_id
        self.opponent_team = opponent_team
        self.opponent_leader_id = opponent_leader_id
        self.guild_id = guild_id
        self.message: discord.Message = None  # set by the caller after sending

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.opponent_leader_id:
            await interaction.response.send_message("This scrim request isn't for you.", ephemeral=True)
            return False
        return True

    def _clear_pending(self):
        # Only clear the challenger's pending slot if it's still tracking *this* request —
        # avoids wiping out a newer request the challenger may have sent after this resolved.
        pending = pending_scrim_requests.get(self.challenger_leader_id)
        if pending and pending.get("opponent_team") == self.opponent_team:
            pending_scrim_requests.pop(self.challenger_leader_id, None)

    @discord.ui.button(label="Yes", style=discord.ButtonStyle.success)
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()

        db = load_db()
        challenger_info = db["teams"].get(self.challenger_team)
        opponent_info = db["teams"].get(self.opponent_team)
        if challenger_info is None or opponent_info is None:
            self._clear_pending()
            for child in self.children:
                child.disabled = True
            await safe_edit_original_response(interaction, content="One of these teams no longer exists.", view=self)
            return

        guild = bot.get_guild(self.guild_id)
        category = guild.get_channel(SCRIM_CATEGORY_ID)
        if category is None:
            self._clear_pending()
            for child in self.children:
                child.disabled = True
            await safe_edit_original_response(interaction, content="The scrim category no longer exists — ask staff to check the bot config.", view=self)
            return

        challenger_role = guild.get_role(challenger_info["role_id"])
        opponent_role = guild.get_role(opponent_info["role_id"])
        challenger_leader = guild.get_member(self.challenger_leader_id) or await guild.fetch_member(self.challenger_leader_id)
        opponent_leader = guild.get_member(self.opponent_leader_id) or await guild.fetch_member(self.opponent_leader_id)

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            guild.me: discord.PermissionOverwrite(
                view_channel=True, send_messages=True, manage_channels=True, mention_everyone=True
            ),
        }
        if challenger_role is not None:
            overwrites[challenger_role] = scrim_member_channel_overwrite()
        if opponent_role is not None:
            overwrites[opponent_role] = scrim_member_channel_overwrite()
        if challenger_leader is not None:
            overwrites[challenger_leader] = scrim_leader_channel_overwrite()
        if opponent_leader is not None:
            overwrites[opponent_leader] = scrim_leader_channel_overwrite()

        try:
            scrim_channel = await guild.create_text_channel(
                name=SCRIM_CHANNEL_NAME,
                category=category,
                overwrites=overwrites,
                reason=f"Scrim accepted between {self.challenger_team} and {self.opponent_team}",
            )
        except discord.HTTPException as e:
            self._clear_pending()
            for child in self.children:
                child.disabled = True
            await safe_edit_original_response(interaction, content=f"Failed to create the scrim channel: {e}", view=self)
            return

        created_dt = discord.utils.utcnow()
        delete_dt = created_dt + timedelta(days=SCRIM_DURATION_DAYS)

        ping_bits = []
        if challenger_role is not None:
            ping_bits.append(challenger_role.mention)
        if opponent_role is not None:
            ping_bits.append(opponent_role.mention)

        await scrim_channel.send(
            f"{' '.join(ping_bits)}\n"
            f"🎯 **{self.challenger_team}** vs **{self.opponent_team}** — the scrim is on!\n"
            f"This channel will automatically be deleted "
            f"{discord.utils.format_dt(delete_dt, style='F')} ({discord.utils.format_dt(delete_dt, style='R')}).",
            allowed_mentions=discord.AllowedMentions(roles=True),
        )

        scrim_db = load_scrim_db()
        scrim_db["scrims"][str(scrim_channel.id)] = {
            "channel_id": scrim_channel.id,
            "guild_id": guild.id,
            "team_a": self.challenger_team,
            "team_a_leader_id": self.challenger_leader_id,
            "team_b": self.opponent_team,
            "team_b_leader_id": self.opponent_leader_id,
            "created_ts": int(created_dt.timestamp()),
            "delete_ts": int(delete_dt.timestamp()),
        }
        save_scrim_db(scrim_db)
        await backup_scrim_db_to_log_channel()

        self._clear_pending()

        await log_team_event(
            "🎯 Scrim Accepted",
            colour=discord.Colour.green(),
            fields=[
                ("Team A", self.challenger_team, True),
                ("Team B", self.opponent_team, True),
                ("Channel", scrim_channel.mention, True),
                ("Deletes", discord.utils.format_dt(delete_dt, style='F'), True),
            ],
        )

        for child in self.children:
            child.disabled = True
        await safe_edit_original_response(
            interaction,
            content=f"Scrim accepted! Channel created: {scrim_channel.mention}",
            view=self,
        )

    @discord.ui.button(label="No", style=discord.ButtonStyle.danger)
    async def decline(self, interaction: discord.Interaction, button: discord.ui.Button):
        self._clear_pending()
        for child in self.children:
            child.disabled = True
        await log_team_event(
            "❌ Scrim Declined",
            colour=discord.Colour.orange(),
            fields=[
                ("Team A", self.challenger_team, True),
                ("Team B", self.opponent_team, True),
            ],
        )
        await interaction.response.edit_message(content="Scrim declined.", view=self)

        challenger_leader = bot.get_user(self.challenger_leader_id)
        try:
            if challenger_leader is not None:
                await challenger_leader.send(f"**{self.opponent_team}** declined your scrim request.")
        except discord.HTTPException:
            pass

    async def on_timeout(self):
        # The opponent leader never responded within 24h — free up the challenger's pending
        # slot and disable the stale buttons, same as an explicit decline.
        self._clear_pending()
        for child in self.children:
            child.disabled = True
        await log_team_event(
            "⌛ Scrim Request Expired",
            colour=discord.Colour.dark_grey(),
            fields=[
                ("Team A", self.challenger_team, True),
                ("Team B", self.opponent_team, True),
            ],
        )
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


class CancelScrimRequestView(discord.ui.View):
    """Shown ephemerally to a leader who tries to send a second scrim request while one is
    already pending — lets them cancel the outstanding one instead."""

    def __init__(self, leader_id: int):
        super().__init__(timeout=60)
        self.leader_id = leader_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.leader_id:
            await interaction.response.send_message("This isn't your scrim request to cancel.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Cancel Request", style=discord.ButtonStyle.danger)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        pending = await _cancel_pending_scrim_request(
            self.leader_id, note="This scrim request was cancelled by the challenger."
        )
        if pending is None:
            await interaction.response.edit_message(content="That request was already resolved.", view=None)
            return
        await log_team_event(
            "🚫 Scrim Request Cancelled",
            colour=discord.Colour.orange(),
            fields=[("Cancelled By", f"<@{self.leader_id}> (`{self.leader_id}`)", True)],
        )
        await interaction.response.edit_message(content="Scrim request cancelled.", view=None)


# ============================================================
# GIVEAWAYS — state, embed, join button, and background ender
# ============================================================

GIVEAWAY_JOIN_EMOJI = "🎉"

# Matches durations like "10m", "2h", "1d", "1d12h", "1w" — one or more (amount, unit) pairs.
_DURATION_RE = re.compile(r"(\d+)\s*(w|d|h|m|s)", re.IGNORECASE)
_DURATION_UNIT_SECONDS = {"w": 604800, "d": 86400, "h": 3600, "m": 60, "s": 1}


def parse_duration(text: str):
    """Parses a duration string like '10m', '2h', or '1d12h' into a timedelta.
    Returns None if nothing valid could be parsed."""
    from datetime import timedelta

    total_seconds = 0
    matched_any = False
    for amount, unit in _DURATION_RE.findall(text.strip()):
        total_seconds += int(amount) * _DURATION_UNIT_SECONDS[unit.lower()]
        matched_any = True

    if not matched_any or total_seconds <= 0:
        return None
    return timedelta(seconds=total_seconds)


_MESSAGE_LINK_RE = re.compile(r"discord(?:app)?\.com/channels/(\d+)/(\d+)/(\d+)")


def parse_message_link(link: str):
    """Parses a Discord message link (…/channels/<guild>/<channel>/<message>) into
    (channel_id, message_id) ints. Returns None if the link doesn't match."""
    match = _MESSAGE_LINK_RE.search(link.strip())
    if not match:
        return None
    _, channel_id, message_id = match.groups()
    return int(channel_id), int(message_id)


def build_giveaway_view(
    prize: str,
    winners_count: int,
    host_id: int,
    end_ts: int,
    entries_count: int,
    winner_ids: list = None,
) -> "GiveawayView":
    """Mirrors the AC: Arena Hub giveaway style — a title, a timestamp for when it
    ends (or ended), the host, a live entry count, and — once it's over — the
    winners — built as a Components V2 container (no accent colour) instead of the
    old embed."""
    return GiveawayView(
        prize=prize,
        winners_count=winners_count,
        host_id=host_id,
        end_ts=end_ts,
        entries_count=entries_count,
        winner_ids=winner_ids,
    )


class GiveawayView(discord.ui.LayoutView):
    """A Components V2 container replacing the old giveaway embed + separate
    GiveawayJoinView — same title, ends/ended timestamp, host, live entry count, and
    (once it's over) the winners, all in one native container with no accent colour.
    The Join button lives inside the container and is only added while the giveaway
    is still running; call with no arguments to get a placeholder used only to
    re-register the persistent Join button's custom_id after a restart (see on_ready)."""

    def __init__(
        self,
        prize: str | None = None,
        winners_count: int = 1,
        host_id: int | None = None,
        end_ts: int = 0,
        entries_count: int = 0,
        winner_ids: list = None,
    ):
        super().__init__(timeout=None)
        ended = winner_ids is not None

        if prize is None:
            # Placeholder registration path — just needs the Join button's custom_id present.
            button = discord.ui.Button(
                label="Join", emoji=GIVEAWAY_JOIN_EMOJI, style=discord.ButtonStyle.success,
                custom_id="giveaway_join_button",
            )
            button.callback = self._make_join_callback(button)
            self.add_item(discord.ui.Container(discord.ui.ActionRow(button)))
            return

        children = [
            discord.ui.TextDisplay(f"# {GIVEAWAY_JOIN_EMOJI} {prize}"),
            discord.ui.Separator(),
        ]
        if ended:
            children.append(discord.ui.TextDisplay(f"**Ended:** <t:{end_ts}:F>"))
        else:
            children.append(discord.ui.TextDisplay(f"**Ends:** <t:{end_ts}:R> (<t:{end_ts}:F>)"))
        children.append(discord.ui.TextDisplay(f"**Hosted by:** <@{host_id}>"))
        children.append(discord.ui.TextDisplay(f"**Entries:** **{entries_count}**"))

        if ended:
            winners_value = (
                ", ".join(f"<@{uid}>" for uid in winner_ids) if winner_ids else "No valid entries"
            )
            label = "Winner" if len(winner_ids) == 1 else "Winners"
            children.append(discord.ui.TextDisplay(f"**{label}:** {winners_value}"))
        else:
            button = discord.ui.Button(
                label="Join", emoji=GIVEAWAY_JOIN_EMOJI, style=discord.ButtonStyle.success,
                custom_id="giveaway_join_button",
            )
            button.callback = self._make_join_callback(button)
            children.append(discord.ui.ActionRow(button))
            children.append(
                discord.ui.TextDisplay(f"-# {winners_count} winner(s) • Click {GIVEAWAY_JOIN_EMOJI} Join above to enter!")
            )

        if os.path.exists(SUPPORT_BANNER_PATH):
            children.append(
                discord.ui.MediaGallery(discord.MediaGalleryItem(media=f"attachment://{SUPPORT_BANNER_FILENAME}"))
            )

        self.add_item(discord.ui.Container(*children))

    def _make_join_callback(self, button: discord.ui.Button):
        async def callback(interaction: discord.Interaction):
            db = load_db()
            giveaways = db.setdefault("giveaways", {})
            key = str(interaction.message.id)
            info = giveaways.get(key)

            if info is None:
                await interaction.response.send_message("This giveaway no longer exists.", ephemeral=True)
                return
            if info.get("ended"):
                await interaction.response.send_message("This giveaway has already ended.", ephemeral=True)
                return

            entries = info.setdefault("entries", [])
            user_id = interaction.user.id
            if user_id in entries:
                entries.remove(user_id)
                joined = False
            else:
                entries.append(user_id)
                joined = True

            save_db(db)

            view = build_giveaway_view(
                prize=info["prize"],
                winners_count=info["winners"],
                host_id=info["host_id"],
                end_ts=info["end_ts"],
                entries_count=len(entries),
            )
            # Acknowledge the click FIRST. Discord invalidates the whole interaction (and every
            # followup after it) if it doesn't get an initial response within 3 seconds — and a
            # popular giveaway can get enough simultaneous clicks to rate-limit the log-channel
            # backup below, which used to run *before* this and could blow past that window.
            try:
                await interaction.response.edit_message(view=view)
            except discord.HTTPException:
                pass

            if joined:
                await interaction.followup.send(f"{GIVEAWAY_JOIN_EMOJI} You're in — good luck!", ephemeral=True)
            else:
                await interaction.followup.send("You left the giveaway.", ephemeral=True)

            # Persist entries so they survive a Railway redeploy — but batched, not on every
            # single click. A popular giveaway can get dozens of clicks a second, and running
            # the full multi-file, multi-channel backup on each one is exactly what caused the
            # rate-limit storm. Mark it dirty here; check_giveaways() flushes it every 30s.
            global _giveaway_backup_dirty
            _giveaway_backup_dirty = True

        return callback


async def _end_giveaway(guild: discord.Guild, message_id: str, info: dict):
    """Picks winners, edits the giveaway message to its final state, and announces the
    result in the same channel."""
    import random

    entries = info.get("entries", [])
    winners_count = min(info.get("winners", 1), len(entries))
    winner_ids = random.sample(entries, winners_count) if winners_count > 0 else []

    info["ended"] = True
    info["winner_ids"] = winner_ids

    channel = guild.get_channel(info["channel_id"]) or bot.get_channel(info["channel_id"])
    if channel is None:
        try:
            channel = await bot.fetch_channel(info["channel_id"])
        except discord.HTTPException:
            return

    view = build_giveaway_view(
        prize=info["prize"],
        winners_count=info["winners"],
        host_id=info["host_id"],
        end_ts=info["end_ts"],
        entries_count=len(entries),
        winner_ids=winner_ids,
    )

    try:
        message = await channel.fetch_message(int(message_id))
        await message.edit(view=view)
    except discord.HTTPException:
        pass

    if winner_ids:
        mentions = ", ".join(f"<@{uid}>" for uid in winner_ids)
        try:
            await channel.send(f"{GIVEAWAY_JOIN_EMOJI} Congratulations {mentions} — you won **{info['prize']}**!")
        except discord.HTTPException:
            pass
    else:
        try:
            await channel.send(f"{GIVEAWAY_JOIN_EMOJI} The giveaway for **{info['prize']}** ended with no entries.")
        except discord.HTTPException:
            pass


@tasks.loop(seconds=30)
async def check_giveaways():
    db = load_db()
    giveaways = db.get("giveaways", {})
    if not giveaways:
        return

    now_ts = int(discord.utils.utcnow().timestamp())
    changed = False

    for message_id, info in list(giveaways.items()):
        if info.get("ended") or info.get("end_ts", 0) > now_ts:
            continue

        guild = bot.get_guild(info.get("guild_id")) if info.get("guild_id") else None
        if guild is None:
            # fall back to the first guild the bot can see the channel in
            channel = bot.get_channel(info["channel_id"])
            guild = channel.guild if channel else None
        if guild is None:
            continue

        await _end_giveaway(guild, message_id, info)
        changed = True

    global _giveaway_backup_dirty
    if changed or _giveaway_backup_dirty:
        if changed:
            save_db(db)
        _giveaway_backup_dirty = False
        await backup_db_to_log_channel()


@check_giveaways.before_loop
async def before_check_giveaways():
    await bot.wait_until_ready()


@tasks.loop(minutes=SCRIM_CHECK_INTERVAL_MINUTES)
async def check_scrim_expiry():
    scrim_db = load_scrim_db()
    scrims = scrim_db.get("scrims", {})
    if not scrims:
        return

    now_ts = int(discord.utils.utcnow().timestamp())
    changed = False

    for channel_id_str, info in list(scrims.items()):
        if info.get("delete_ts", 0) > now_ts:
            continue

        guild = bot.get_guild(info.get("guild_id"))
        channel = guild.get_channel(info["channel_id"]) if guild else bot.get_channel(info["channel_id"])
        if channel is not None:
            try:
                await channel.delete(reason=f"Scrim between {info.get('team_a')} and {info.get('team_b')} expired")
            except discord.HTTPException:
                pass

        scrims.pop(channel_id_str, None)
        changed = True

        await log_team_event(
            "🗑️ Scrim Channel Auto-Deleted",
            colour=discord.Colour.dark_grey(),
            fields=[
                ("Team A", info.get("team_a", "Unknown"), True),
                ("Team B", info.get("team_b", "Unknown"), True),
            ],
        )

    if changed:
        save_scrim_db(scrim_db)
        await backup_scrim_db_to_log_channel()


@check_scrim_expiry.before_loop
async def before_check_scrim_expiry():
    await bot.wait_until_ready()


# ============================================================
# META QUEST UPDATE TRACKER — watches the Animal Company store
# page and posts an embed to META_UPDATE_CHANNEL_ID whenever the
# version number changes.
# ============================================================

def load_last_meta_version() -> str | None:
    data = _load_json_file(META_VERSION_DB_FILE)
    if data is not None:
        return data.get("last_version") or None

    # One-time migration: an older build of this bot stored the version as plain text
    # in META_VERSION_FILE with no channel backup at all, so a redeploy silently wiped
    # it (Railway wipes the container's disk on every redeploy). If that legacy file is
    # still lying around locally, read it once so this rollout doesn't fire a spurious
    # "update detected" — save_last_meta_version() below will write the JSON version.
    if os.path.exists(META_VERSION_FILE):
        with open(META_VERSION_FILE, "r", encoding="utf-8") as f:
            v = f.read().strip()
        return v or None

    return None


def save_last_meta_version(version: str) -> None:
    _atomic_write_json(
        META_VERSION_DB_FILE,
        {"last_version": version, "last_updated": discord.utils.utcnow().isoformat()},
    )


async def backup_meta_version_to_log_channel():
    try:
        await _backup_file_to_channel(META_LOG_CHANNEL_ID, META_VERSION_DB_FILE, META_VERSION_DB_FILE)
    except discord.HTTPException as e:
        print(f"Failed to back up meta version db to log channel: {e}")
    except Exception as e:  # noqa: BLE001 - never let a bad backup attempt kill the poll loop
        print(f"Unexpected error backing up meta version db: {e}")


async def restore_meta_version_from_log_channel():
    """Pulls the last-logged version JSON from META_LOG_CHANNEL_ID into local storage on
    startup — critical because Railway wipes the container's disk on every redeploy,
    the same reason teams/giveaways/tickets are backed up this way. Falls back to
    migrating the legacy local .txt file (if present) and immediately pushing a fresh
    backup, so no previously-known version is lost."""
    if _load_json_file(META_VERSION_DB_FILE) is not None:
        # Local data already present (e.g. a crash-restart, not a fresh container) —
        # push it straight to the log channel so the backup there is confirmed up to date.
        await backup_meta_version_to_log_channel()
        return

    try:
        found = await _restore_file_from_channel(META_LOG_CHANNEL_ID, META_VERSION_DB_FILE, META_VERSION_DB_FILE)
    except discord.HTTPException as e:
        print(f"Failed to restore meta version db from log channel: {e}")
        found = False

    if found:
        print("Restored last-logged Meta version from log channel backup.")
        return

    # No backup in the channel yet — fall back to the legacy local .txt file, if any,
    # migrate it into the JSON format, and push the first backup right away.
    if os.path.exists(META_VERSION_FILE):
        with open(META_VERSION_FILE, "r", encoding="utf-8") as f:
            legacy_version = f.read().strip()
        if legacy_version:
            save_last_meta_version(legacy_version)
            await backup_meta_version_to_log_channel()
            print("Migrated last-logged Meta version from the legacy local file.")
            return

    print("No existing last-logged Meta version found — starting fresh.")


def _sanitize_version_text(text: str | None, max_len: int = 1000) -> str | None:
    """Remove HTML tags, collapse whitespace, and truncate to max_len."""
    if text is None:
        return None
    s = str(text)
    # Remove obvious HTML tags if present
    s = re.sub(r"<[^>]+>", "", s)
    # Collapse whitespace
    s = re.sub(r"\s+", " ", s).strip()
    if len(s) > max_len:
        return s[: max_len - 1] + "…"
    return s


# Only accepts an actual version-number shape (e.g. "1.85.2.3320") immediately after the
# word "Version" — NOT just any text. The raw (pre-JS-render) HTML aiohttp fetches below
# often contains the literal word "version" buried in unrelated embedded JS/config blobs
# (analytics config, DTSG tokens, etc.); a loose regex would grab whatever text follows
# THAT instead of the real game version, and since *a* match was found the old code
# returned immediately without ever trying the reliable Playwright (real browser) path.
# Requiring a numeric X.Y[.Z[.W]] shape makes false positives on that kind of JS/JSON
# noise extremely unlikely, so a non-match here correctly falls through to Playwright.
_VERSION_NUMBER_RE = re.compile(r"\bVersion\b[:\s\-–—]*([0-9]+(?:\.[0-9]+){1,4})", re.IGNORECASE)


async def fetch_meta_version() -> str | None:
    """Try to fetch the Meta store page and scrape the 'Version' text.
    First tries a lightweight aiohttp request + a strict version-number regex; falls back
    to Playwright (which renders the page's JS, same as a real browser) whenever that
    strict match isn't found — which, since the store page is client-rendered, is the
    common case. Returns the sanitized, truncated version string or None on failure.
    """
    # 1) Try aiohttp + a strict regex (fast, avoids needing Playwright / a browser on
    # Railway) — but only trust it if it actually looks like a version number.
    try:
        timeout = aiohttp.ClientTimeout(total=15)
        headers = {
            "User-Agent": "Mozilla/5.0 (compatible; AC-UpdateBot/1.0; +https://example.org/bot)"
        }
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            async with session.get(META_URL) as resp:
                if resp.status != 200:
                    print(f"[DEBUG] fetch_meta_version: HTTP {resp.status} from {META_URL}")
                else:
                    text = await resp.text()
                    m = _VERSION_NUMBER_RE.search(text)
                    if m:
                        return _sanitize_version_text(m.group(1), max_len=100)
                    print(
                        "[DEBUG] fetch_meta_version: no version-number-shaped match in the raw "
                        "HTML (expected — the page is client-rendered) — falling back to Playwright."
                    )
    except Exception as e:
        print(f"[DEBUG] aiohttp attempt failed: {e}")

    # 2) Fallback to Playwright (only if aiohttp didn't find it)
    try:
        async with async_playwright() as p:
            # --no-sandbox helps in many restricted containers; if it causes issues remove it.
            browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
            page = await browser.new_page()
            try:
                await page.goto(META_URL, wait_until="networkidle", timeout=20000)
            except Exception:
                # Some pages never hit networkidle; retry without waiting for networkidle
                try:
                    await page.goto(META_URL, timeout=20000)
                except Exception as e:
                    await browser.close()
                    print(f"[ERROR] Playwright failed to navigate to {META_URL}: {e}")
                    return None

            try:
                version = await page.evaluate(
                    """() => {
                        // Only accept an actual version-number shape (e.g. "1.85.2.3320")
                        // right after the word "Version" — same strictness as the aiohttp
                        // regex, so a stray "version" elsewhere on the page can't be
                        // mistaken for the real one.
                        const versionShape = /^[:\s\-\u2013\u2014]*([0-9]+(?:\.[0-9]+){1,4})/;
                        const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, null);
                        let node;
                        while (node = walker.nextNode()) {
                            const t = node.textContent.trim();
                            if (!t) continue;
                            const idx = t.toLowerCase().indexOf('version');
                            if (idx !== -1) {
                                const after = t.slice(idx + 'version'.length);
                                const match = after.match(versionShape);
                                if (match) return match[1];
                            }
                        }
                        const divs = [...document.querySelectorAll('div, span, p')];
                        for (const el of divs) {
                            const t = (el.innerText || '').trim();
                            if (t.toLowerCase().startsWith('version')) {
                                const match = t.slice('version'.length).match(versionShape);
                                if (match) return match[1];
                            }
                        }
                        return null;
                    }"""
                )
            finally:
                await browser.close()
            return _sanitize_version_text(version, max_len=100)
    except Exception as e:
        print(f"[ERROR] Failed to fetch Meta version (Playwright): {e}")
        return None


class MetaUpdateView(discord.ui.LayoutView):
    """A Components V2 container styled like the old 'Update Detected!' embed — same
    author eyebrow, title, timestamp/game name, two version fields, and (if the banner
    was scraped) the banner image, just built out of native container components.
    If ping_role_id is given, the role mention is included as its own component at the
    top of the container, since Components V2 messages can't use a top-level content field."""

    def __init__(
        self,
        current: str,
        previous: str | None,
        detected_ts: int,
        include_banner: bool,
        ping_role_id: int | None = None,
    ):
        super().__init__(timeout=None)
        current_display = _sanitize_version_text(current, max_len=1000) or "Unknown"
        previous_display = _sanitize_version_text(previous or current, max_len=1000) or "Unknown"

        children = []
        if ping_role_id is not None:
            # Components V2 messages can't carry a top-level `content` field, so the
            # role ping has to live inside the container as its own text component —
            # putting it in `content` alongside a LayoutView makes Discord reject the send.
            children.append(discord.ui.TextDisplay(f"<@&{ping_role_id}>"))
        children += [
            discord.ui.TextDisplay(f"-# {META_EMBED_AUTHOR}"),
            discord.ui.TextDisplay(
                f"# Update Detected!\n<t:{detected_ts}:F> ( <t:{detected_ts}:R> )\n**{META_GAME_DISPLAY_NAME}**"
            ),
            discord.ui.Separator(),
            discord.ui.TextDisplay(f"🟢 | **Updated Version:**\n```{current_display}```"),
            discord.ui.TextDisplay(f"🔴 | **Last Logged:**\n```{previous_display}```"),
        ]
        if include_banner:
            children.append(
                discord.ui.MediaGallery(discord.MediaGalleryItem(media=f"attachment://{SUPPORT_BANNER_FILENAME}"))
            )

        container = discord.ui.Container(*children)
        self.add_item(container)


async def check_for_meta_update() -> tuple[bool, str | None, str | None]:
    """Checks the store for a new version and, if it changed, posts the update container
    (with support_banner.png attached, same image used on the ticket panel and giveaway
    embeds) to META_UPDATE_CHANNEL_ID. Returns (changed, current_version, previous_version)."""
    previous = load_last_meta_version()
    current = await fetch_meta_version()

    if current and current != previous:
        save_last_meta_version(current)
        await backup_meta_version_to_log_channel()
        channel = bot.get_channel(META_UPDATE_CHANNEL_ID) or await bot.fetch_channel(META_UPDATE_CHANNEL_ID)
        if channel:
            detected_ts = int(discord.utils.utcnow().timestamp())

            file = None
            include_banner = os.path.exists(SUPPORT_BANNER_PATH)
            if include_banner:
                file = discord.File(SUPPORT_BANNER_PATH, filename=SUPPORT_BANNER_FILENAME)
            else:
                print(f"Support banner image missing at {SUPPORT_BANNER_PATH} — update message sent without image.")

            view = MetaUpdateView(current, previous, detected_ts, include_banner, ping_role_id=META_UPDATE_PING_ROLE_ID)

            try:
                if file is not None:
                    await channel.send(view=view, file=file)
                else:
                    await channel.send(view=view)
            except discord.HTTPException as e:
                # If the container fails (still too large or otherwise invalid), fall back to a plaintext summary.
                print(f"[ERROR] Failed to send meta update message: {e}")
                try:
                    short_current = _sanitize_version_text(current, max_len=800)
                    short_previous = _sanitize_version_text(previous or current, max_len=800)
                    fallback_msg = (
                        f"<@&{META_UPDATE_PING_ROLE_ID}> Meta Update Detected!\n\nUpdated Version: {short_current}\n"
                        f"Last Logged: {short_previous or 'None'}"
                    )
                    await channel.send(content=fallback_msg)
                except Exception as e2:
                    print(f"[ERROR] Failed to send fallback meta update message: {e2}")
        return True, current, previous

    return False, current, previous


@tasks.loop(minutes=META_CHECK_INTERVAL_MINUTES)
async def meta_poll_loop():
    await check_for_meta_update()


@meta_poll_loop.before_loop
async def before_meta_poll_loop():
    await bot.wait_until_ready()


@bot.tree.command(name="checkupdate", description="Checks for an Animal Company update manually")
async def checkupdate(interaction: discord.Interaction):
    await interaction.response.defer()
    changed, current, previous = await check_for_meta_update()

    if current is None:
        await interaction.followup.send("⚠️ Couldn't fetch the version from the Meta store page.")
        return

    if changed:
        await interaction.followup.send(
            f"✅ Update detected!\nCurrent: `{current}`\nPrevious: `{previous or 'None'}`"
        )
    else:
        await interaction.followup.send(f"No update detected.\nCurrent: `{current}`")


@bot.tree.command(
    name="updateembed",
    description="(Staff) Preview the update message's current look — doesn't save anything",
)
async def updateembed(interaction: discord.Interaction):
    await interaction.response.defer()

    if not has_staff_role(interaction.user):
        await interaction.followup.send("You don't have permission to use this command.", ephemeral=True)
        return

    current = await fetch_meta_version()
    if current is None:
        await interaction.followup.send(
            "⚠️ Couldn't fetch the version from the Meta store page — nothing to preview.", ephemeral=True
        )
        return

    previous = load_last_meta_version()
    detected_ts = int(discord.utils.utcnow().timestamp())

    file = None
    include_banner = os.path.exists(SUPPORT_BANNER_PATH)
    if include_banner:
        file = discord.File(SUPPORT_BANNER_PATH, filename=SUPPORT_BANNER_FILENAME)

    view = MetaUpdateView(current, previous, detected_ts, include_banner)

    if file is not None:
        await interaction.followup.send(view=view, file=file)
    else:
        await interaction.followup.send(view=view)


# ============================================================
# WEEKLY MESSAGE LEADERBOARD — tracks how many messages each
# member sends and posts a top-10 card via /messageleaderboard.
# Counts reset automatically every Sunday at 23:59 UTC.
# ============================================================

def _message_leaderboard_reset_boundary(now: datetime) -> datetime:
    """Returns the most recent Sunday-23:59-UTC boundary at or before `now`. Counts
    belong to the "week" starting at whichever boundary is currently in effect, so a
    reset just means: if a newer boundary has been crossed, wipe the counts."""
    days_since = (now.weekday() - MESSAGE_LEADERBOARD_RESET_WEEKDAY) % 7
    candidate = (now - timedelta(days=days_since)).replace(
        hour=MESSAGE_LEADERBOARD_RESET_HOUR_UTC,
        minute=MESSAGE_LEADERBOARD_RESET_MINUTE_UTC,
        second=0,
        microsecond=0,
    )
    if candidate > now:
        candidate -= timedelta(days=7)
    return candidate


def _message_leaderboard_next_reset(now: datetime) -> datetime:
    return _message_leaderboard_reset_boundary(now) + timedelta(days=7)


def record_message_for_leaderboard(user_id: int) -> None:
    """Called from on_message for every non-bot message sent in MESSAGE_LEADERBOARD_TRACKED_CHANNEL_ID.
    Does NOT reset on its own — message_leaderboard_rollover_loop owns the weekly reset
    (and the role hand-out that goes with it) so there's exactly one place that can trigger
    it. If this fired with no period set yet (very first run before the loop's first tick),
    just start one rather than losing the message."""
    global _message_leaderboard_period_start, _message_leaderboard_dirty

    if _message_leaderboard_period_start is None:
        _message_leaderboard_period_start = _message_leaderboard_reset_boundary(discord.utils.utcnow())

    _message_leaderboard_counts[user_id] = _message_leaderboard_counts.get(user_id, 0) + 1
    _message_leaderboard_dirty = True


def save_message_leaderboard_db() -> None:
    _atomic_write_json(
        MESSAGE_LEADERBOARD_DB_FILE,
        {
            "counts": {str(uid): count for uid, count in _message_leaderboard_counts.items()},
            "period_start": (
                _message_leaderboard_period_start.isoformat()
                if _message_leaderboard_period_start
                else None
            ),
            "last_updated": discord.utils.utcnow().isoformat(),
        },
    )


def load_message_leaderboard_db() -> None:
    global _message_leaderboard_period_start
    data = _load_json_file(MESSAGE_LEADERBOARD_DB_FILE)
    if data is None:
        return

    _message_leaderboard_counts.clear()
    for uid, count in (data.get("counts") or {}).items():
        _message_leaderboard_counts[int(uid)] = count

    period_start = data.get("period_start")
    _message_leaderboard_period_start = (
        datetime.fromisoformat(period_start) if period_start else None
    )

    # If we've been offline across a weekly boundary, reset now rather than waiting for the
    # rollover loop's first tick — keeps a stale prior week from lingering silently. Note:
    # since this runs before the loop (and before we can reliably touch guild roles) it just
    # wipes the counts — the top-3/top-10 role hand-out for that missed week doesn't happen,
    # the same trade-off other offline-spanning resets in this bot already make.
    now = discord.utils.utcnow()
    boundary = _message_leaderboard_reset_boundary(now)
    if _message_leaderboard_period_start is None or boundary > _message_leaderboard_period_start:
        _message_leaderboard_counts.clear()
        _message_leaderboard_period_start = boundary


def _message_leaderboard_guild() -> discord.Guild | None:
    """Resolves the single guild this bot operates in via the tracked channel, since the
    weekly rollover runs from a background task with no interaction to pull a guild from."""
    channel = bot.get_channel(MESSAGE_LEADERBOARD_TRACKED_CHANNEL_ID)
    return channel.guild if channel else None


async def _apply_weekly_leaderboard_roles(top3_ids: list[int], top10_ids: list[int]) -> None:
    """Strips both weekly leaderboard roles from everyone holding them, then hands the
    top-10 role to #1-#10 and the top-3 role to #1-#3 (so the top 3 end up with both).
    Called right after counts are snapshotted and reset for the new week."""
    guild = _message_leaderboard_guild()
    if guild is None:
        print("[leaderboard] Couldn't resolve the guild for the weekly role rollover — skipping role changes.")
        return

    top3_role = guild.get_role(MESSAGE_LEADERBOARD_TOP3_ROLE_ID)
    top10_role = guild.get_role(MESSAGE_LEADERBOARD_TOP10_ROLE_ID)
    if top3_role is None or top10_role is None:
        print("[leaderboard] Weekly leaderboard role(s) not found — check the role ID constants.")
        return

    for role in (top3_role, top10_role):
        for member in list(role.members):
            try:
                await member.remove_roles(role, reason="Weekly message leaderboard reset")
            except discord.HTTPException as e:
                print(f"[leaderboard] Failed to remove {role.name} from {member.id}: {e}")

    for user_id in top10_ids:
        member = guild.get_member(user_id)
        if member is None:
            continue
        try:
            await member.add_roles(top10_role, reason="Weekly message leaderboard — top 10")
        except discord.HTTPException as e:
            print(f"[leaderboard] Failed to add the top-10 role to {user_id}: {e}")

    for user_id in top3_ids:
        member = guild.get_member(user_id)
        if member is None:
            continue
        try:
            await member.add_roles(top3_role, reason="Weekly message leaderboard — top 3")
        except discord.HTTPException as e:
            print(f"[leaderboard] Failed to add the top-3 role to {user_id}: {e}")


async def backup_message_leaderboard_to_log_channel():
    try:
        await _backup_file_to_channel(
            MESSAGE_LEADERBOARD_LOG_CHANNEL_ID, MESSAGE_LEADERBOARD_DB_FILE, MESSAGE_LEADERBOARD_DB_FILE
        )
    except discord.HTTPException as e:
        print(f"Failed to back up message leaderboard db to log channel: {e}")
    except Exception as e:  # noqa: BLE001 - never let a bad backup attempt kill the save loop
        print(f"Unexpected error backing up message leaderboard db: {e}")


async def restore_message_leaderboard_from_log_channel():
    """Pulls the last-known counts from MESSAGE_LEADERBOARD_LOG_CHANNEL_ID into local
    storage on startup — critical because Railway wipes the container's disk on every
    redeploy, the same reason teams/giveaways/tickets/meta-version are backed up this way."""
    if _load_json_file(MESSAGE_LEADERBOARD_DB_FILE) is not None:
        load_message_leaderboard_db()
        await backup_message_leaderboard_to_log_channel()
        return

    try:
        found = await _restore_file_from_channel(
            MESSAGE_LEADERBOARD_LOG_CHANNEL_ID, MESSAGE_LEADERBOARD_DB_FILE, MESSAGE_LEADERBOARD_DB_FILE
        )
    except discord.HTTPException as e:
        print(f"Failed to restore message leaderboard db from log channel: {e}")
        found = False

    if found:
        load_message_leaderboard_db()
        print("Restored weekly message leaderboard counts from log channel backup.")
    else:
        global _message_leaderboard_period_start
        _message_leaderboard_period_start = _message_leaderboard_reset_boundary(discord.utils.utcnow())
        print("No message leaderboard backup found — starting a fresh week.")


@tasks.loop(minutes=MESSAGE_LEADERBOARD_SAVE_INTERVAL_MINUTES)
async def message_leaderboard_save_loop():
    global _message_leaderboard_dirty
    if not _message_leaderboard_dirty:
        return
    _message_leaderboard_dirty = False
    save_message_leaderboard_db()
    await backup_message_leaderboard_to_log_channel()


@message_leaderboard_save_loop.before_loop
async def before_message_leaderboard_save_loop():
    await bot.wait_until_ready()


@tasks.loop(minutes=1)
async def message_leaderboard_rollover_loop():
    """Checks once a minute for the weekly Sunday-23:59-UTC boundary. When it's crossed,
    snapshots the top 3/top 10 BEFORE wiping counts, resets immediately (no awaits in
    between, so nothing can race in and either see stale counts or miss the reset), then
    hands out the two leaderboard roles based on that snapshot."""
    global _message_leaderboard_period_start, _message_leaderboard_dirty

    now = discord.utils.utcnow()
    boundary = _message_leaderboard_reset_boundary(now)
    if _message_leaderboard_period_start is None or boundary <= _message_leaderboard_period_start:
        return

    ranked = sorted(_message_leaderboard_counts.items(), key=lambda kv: kv[1], reverse=True)
    top3_ids = [uid for uid, _count in ranked[:3]]
    top10_ids = [uid for uid, _count in ranked[:10]]

    _message_leaderboard_counts.clear()
    _message_leaderboard_period_start = boundary
    _message_leaderboard_dirty = True
    save_message_leaderboard_db()

    await backup_message_leaderboard_to_log_channel()
    await _apply_weekly_leaderboard_roles(top3_ids, top10_ids)


@message_leaderboard_rollover_loop.before_loop
async def before_message_leaderboard_rollover_loop():
    await bot.wait_until_ready()


# ============================================================
# ALL-TIME MESSAGE LEADERBOARD — same idea as the weekly one above,
# but counts every non-bot message sent anywhere in the guild and
# never resets. Selectable via the `select` option on /messageleaderboard and
# /messages, plus /syncglobalmessages to rescan the whole server's history.
# ============================================================

def record_overall_message(user_id: int) -> None:
    """Called from on_message for every non-bot message sent anywhere in the guild
    (unlike the weekly tracker, not limited to one channel)."""
    global _overall_message_dirty
    _overall_message_counts[user_id] = _overall_message_counts.get(user_id, 0) + 1
    _overall_message_dirty = True


def save_overall_message_db() -> None:
    _atomic_write_json(
        OVERALL_MESSAGE_DB_FILE,
        {
            "counts": {str(uid): count for uid, count in _overall_message_counts.items()},
            "last_updated": discord.utils.utcnow().isoformat(),
        },
    )


def load_overall_message_db() -> None:
    data = _load_json_file(OVERALL_MESSAGE_DB_FILE)
    if data is None:
        return
    _overall_message_counts.clear()
    for uid, count in (data.get("counts") or {}).items():
        _overall_message_counts[int(uid)] = count


async def backup_overall_message_to_log_channel():
    try:
        await _backup_file_to_channel(
            OVERALL_MESSAGE_LOG_CHANNEL_ID, OVERALL_MESSAGE_DB_FILE, OVERALL_MESSAGE_DB_FILE
        )
    except discord.HTTPException as e:
        print(f"Failed to back up overall message db to log channel: {e}")
    except Exception as e:  # noqa: BLE001 - never let a bad backup attempt kill the save loop
        print(f"Unexpected error backing up overall message db: {e}")


async def restore_overall_message_from_log_channel():
    """Pulls the last-known all-time counts from OVERALL_MESSAGE_LOG_CHANNEL_ID into local
    storage on startup — same reasoning as restore_message_leaderboard_from_log_channel:
    Railway wipes the container's disk on every redeploy."""
    if _load_json_file(OVERALL_MESSAGE_DB_FILE) is not None:
        load_overall_message_db()
        await backup_overall_message_to_log_channel()
        return

    try:
        found = await _restore_file_from_channel(
            OVERALL_MESSAGE_LOG_CHANNEL_ID, OVERALL_MESSAGE_DB_FILE, OVERALL_MESSAGE_DB_FILE
        )
    except discord.HTTPException as e:
        print(f"Failed to restore overall message db from log channel: {e}")
        found = False

    if found:
        load_overall_message_db()
        print("Restored all-time message counts from log channel backup.")
    else:
        print("No all-time message backup found — starting fresh.")


@tasks.loop(minutes=OVERALL_MESSAGE_SAVE_INTERVAL_MINUTES)
async def overall_message_save_loop():
    global _overall_message_dirty
    if not _overall_message_dirty:
        return
    _overall_message_dirty = False
    save_overall_message_db()
    await backup_overall_message_to_log_channel()


@overall_message_save_loop.before_loop
async def before_overall_message_save_loop():
    await bot.wait_until_ready()


# ============================================================
# CHAT STREAKS — three phases per user, driven by "phase_end":
#   "counting" — an open window where STREAK_MESSAGES_REQUIRED messages must land before
#                phase_end (STREAK_WINDOW_HOURS after the window opened) or the streak is lost.
#                Brand-new users start here immediately (no wait needed for day 1).
#   "waiting"  — the gap after successfully banking a day. Messages don't count yet; nothing
#                happens until phase_end (STREAK_WINDOW_HOURS later), at which point a fresh
#                counting window opens. This is what enforces "come back after X (in a day)" —
#                without it, someone chatting continuously could bank a new day every few
#                messages instead of at most once every real 24h.
#   "lost"     — grace period after a missed window. phase_end is the /revivestreak deadline
#                (STREAK_REVIVE_WINDOW_HOURS), capped at STREAK_REVIVE_MONTHLY_LIMIT uses/month.
#                If it expires unused, the streak silently resets to zero.
# Any non-bot message anywhere in the guild counts toward the counting window (same 5s
# anti-spam gate + activity-tracking opt-out as the message leaderboards, since this is
# hooked into that same gated block in on_message).
# ============================================================

def _new_streak_entry() -> dict:
    return {
        "streak_days": 0,
        "phase": "counting",
        "phase_end": None,
        "messages_in_window": 0,
        "revive_month": None,
    }


def load_streak_db() -> dict:
    data = _load_json_file(STREAK_DB_FILE)
    if data is None:
        return {"users": {}}
    data.setdefault("users", {})
    return data


def save_streak_db() -> None:
    _atomic_write_json(
        STREAK_DB_FILE,
        {"users": _streak_data, "last_updated": discord.utils.utcnow().isoformat()},
    )


def _load_streak_cache_from_db(data: dict) -> None:
    _streak_data.clear()
    for uid, entry in data.get("users", {}).items():
        merged = _new_streak_entry()
        merged.update(entry)
        _streak_data[int(uid)] = merged


async def backup_streak_db_to_log_channel():
    try:
        await _backup_file_to_channel(STREAK_LOG_CHANNEL_ID, STREAK_DB_FILE, STREAK_DB_FILE)
    except discord.HTTPException as e:
        print(f"Failed to back up streak db to log channel: {e}")
    except Exception as e:  # noqa: BLE001 - never let a bad backup attempt kill a command
        print(f"Unexpected error backing up streak db: {e}")


async def restore_streak_db_from_log_channel():
    if _load_json_file(STREAK_DB_FILE) is not None:
        _load_streak_cache_from_db(load_streak_db())
        await backup_streak_db_to_log_channel()
        return
    try:
        found = await _restore_file_from_channel(STREAK_LOG_CHANNEL_ID, STREAK_DB_FILE, STREAK_DB_FILE)
    except discord.HTTPException as e:
        print(f"Failed to restore streak db from log channel: {e}")
        return
    if found:
        _load_streak_cache_from_db(load_streak_db())
        print("Restored streak db from log channel backup.")
    else:
        print("No existing streak db backup found — starting fresh.")


def _streak_current_month(now: datetime) -> str:
    return f"{now.year:04d}-{now.month:02d}"


async def _send_streak_channel_message(content: str) -> None:
    try:
        channel = bot.get_channel(STREAK_ANNOUNCE_CHANNEL_ID) or await bot.fetch_channel(STREAK_ANNOUNCE_CHANNEL_ID)
        if channel is not None:
            await channel.send(content)
    except discord.HTTPException as e:
        print(f"Failed to send streak announcement: {e}")


def _streak_guild() -> discord.Guild | None:
    """Resolves the single guild this bot operates in via the announce channel, since streak
    role syncs can fire from a background task with no interaction to pull a guild from."""
    channel = bot.get_channel(STREAK_ANNOUNCE_CHANNEL_ID)
    return channel.guild if channel else None


async def _sync_streak_roles(user_id: int, streak_days: int, phase: str) -> None:
    """Brings a member's streak roles in line with target_streak_role_ids() for their current
    streak_days/phase — adds any newly-earned milestone roles and strips every streak role at
    once when the streak is lost. Recomputes from scratch each call (rather than tracking a
    delta) so a missed sync earlier just gets corrected next time this runs."""
    guild = _streak_guild()
    if guild is None:
        print("[streak] Couldn't resolve the guild for streak role sync — skipping role changes.")
        return
    member = guild.get_member(user_id)
    if member is None:
        return

    target_ids = target_streak_role_ids(streak_days, phase)
    current_ids = {role.id for role in member.roles if role.id in STREAK_ROLE_IDS}

    to_add = [guild.get_role(rid) for rid in (target_ids - current_ids)]
    to_add = [role for role in to_add if role is not None]
    to_remove = [guild.get_role(rid) for rid in (current_ids - target_ids)]
    to_remove = [role for role in to_remove if role is not None]

    if to_add:
        try:
            await member.add_roles(*to_add, reason=f"Chat streak — {streak_days} day(s)")
        except discord.HTTPException as e:
            print(f"[streak] Failed to add streak role(s) to {user_id}: {e}")

    if to_remove:
        try:
            await member.remove_roles(*to_remove, reason="Chat streak lost")
        except discord.HTTPException as e:
            print(f"[streak] Failed to remove streak role(s) from {user_id}: {e}")


async def _advance_streak_phase(user_id: int, entry: dict, now: datetime) -> bool:
    """Moves entry to whichever phase it should be in given the current time — opening the
    next counting window once the wait is over, marking a missed window as lost, or
    silently resetting a streak whose revive grace period ran out. Time-driven (not
    message-driven) so it works the same whether called from record_streak_message or the
    periodic streak_check_loop backstop. Returns True if anything changed."""
    if entry["phase"] == "waiting" and entry["phase_end"] is not None:
        if now >= datetime.fromisoformat(entry["phase_end"]):
            entry["phase"] = "counting"
            entry["phase_end"] = (now + timedelta(hours=STREAK_WINDOW_HOURS)).isoformat()
            entry["messages_in_window"] = 0
            return True
        return False

    if entry["phase"] == "counting" and entry["phase_end"] is not None:
        if now >= datetime.fromisoformat(entry["phase_end"]) and entry["messages_in_window"] < STREAK_MESSAGES_REQUIRED:
            entry["phase"] = "lost"
            entry["phase_end"] = (now + timedelta(hours=STREAK_REVIVE_WINDOW_HOURS)).isoformat()
            entry["messages_in_window"] = 0
            await _send_streak_channel_message(
                f"{STREAK_LEAVE_EMOJI} <@{user_id}>, you've lost your message streak!\n"
                f"*(Psst... if you have your monthly revive available, you have "
                f"{STREAK_REVIVE_WINDOW_HOURS} hours to use `/revivestreak`!)*"
            )
            await _sync_streak_roles(user_id, entry["streak_days"], entry["phase"])
            return True
        return False

    if entry["phase"] == "lost" and entry["phase_end"] is not None:
        if now >= datetime.fromisoformat(entry["phase_end"]):
            revive_month = entry.get("revive_month")
            entry.clear()
            entry.update(_new_streak_entry())
            entry["revive_month"] = revive_month
            await _sync_streak_roles(user_id, entry["streak_days"], entry["phase"])
            return True
        return False

    return False


async def record_streak_message(user_id: int) -> None:
    """Called from on_message for every message that already passed the shared anti-spam +
    opt-out gate. Advances the user's streak phase, banking/announcing a new streak day once
    STREAK_MESSAGES_REQUIRED lands inside an open counting window. Messages sent during the
    post-bank "waiting" phase or the "lost" grace period don't count toward anything — that's
    what stops a streak from being banked more than once every STREAK_WINDOW_HOURS.

    Only touches in-memory state + the dirty flag — never saves or backs up here. This runs
    on every single tracked chat message, and backup_streak_db_to_log_channel() scans channel
    history and hits the Discord API; doing that per-message is exactly what got the bot
    Cloudflare rate-limited. streak_check_loop flushes dirty state periodically instead, same
    as record_message_for_leaderboard/record_overall_message do for their trackers."""
    global _streak_dirty
    now = discord.utils.utcnow()
    entry = _streak_data.setdefault(user_id, _new_streak_entry())

    if await _advance_streak_phase(user_id, entry, now):
        _streak_dirty = True

    if entry["phase"] != "counting":
        return

    if entry["phase_end"] is None:
        entry["phase_end"] = (now + timedelta(hours=STREAK_WINDOW_HOURS)).isoformat()
    entry["messages_in_window"] += 1
    _streak_dirty = True

    if entry["messages_in_window"] >= STREAK_MESSAGES_REQUIRED:
        entry["streak_days"] += 1
        entry["messages_in_window"] = 0
        entry["phase"] = "waiting"
        wait_end = now + timedelta(hours=STREAK_WINDOW_HOURS)
        entry["phase_end"] = wait_end.isoformat()

        ts = int(wait_end.timestamp())
        day_word = "Day" if entry["streak_days"] == 1 else "Days"
        await _send_streak_channel_message(
            f"{STREAK_GAIN_EMOJI} <@{user_id}>, you've accumulated a chat streak!\n"
            f"Come back and chat after <t:{ts}:t> (<t:{ts}:R>) to get your next streak.\n"
            f"**Message Streak:** {entry['streak_days']} {day_word}"
        )
        await _sync_streak_roles(user_id, entry["streak_days"], entry["phase"])


@tasks.loop(minutes=STREAK_CHECK_INTERVAL_MINUTES)
async def streak_check_loop():
    """Runs the time-driven phase check for every user (catches expired windows/waits/revive
    grace periods for anyone who's gone quiet) AND is the only place that flushes streak data
    to disk + Discord — mirrors message_leaderboard_save_loop's dirty-flag pattern so writes
    happen on a timer instead of once per chat message."""
    global _streak_dirty
    if not _streak_data and not _streak_dirty:
        return

    now = discord.utils.utcnow()

    for user_id, entry in list(_streak_data.items()):
        if await _advance_streak_phase(user_id, entry, now):
            _streak_dirty = True

    if not _streak_dirty:
        return
    _streak_dirty = False
    save_streak_db()
    await backup_streak_db_to_log_channel()


@streak_check_loop.before_loop
async def before_streak_check_loop():
    await bot.wait_until_ready()


@bot.tree.command(name="revivestreak", description="Revive a chat streak you recently lost")
async def revivestreak(interaction: discord.Interaction):
    now = discord.utils.utcnow()
    entry = _streak_data.get(interaction.user.id)

    if entry is None or entry["phase"] != "lost":
        await interaction.response.send_message("You don't have a lost streak to revive right now.", ephemeral=True)
        return

    revive_deadline = datetime.fromisoformat(entry["phase_end"])
    if now >= revive_deadline:
        await interaction.response.send_message(
            "Your revive window for that lost streak has expired.", ephemeral=True
        )
        return

    current_month = _streak_current_month(now)
    if entry.get("revive_month") == current_month:
        await interaction.response.send_message(
            "You've already used your monthly revive — you'll be able to revive again next month.",
            ephemeral=True,
        )
        return

    entry["phase"] = "counting"
    entry["messages_in_window"] = 0
    entry["phase_end"] = (now + timedelta(hours=STREAK_WINDOW_HOURS)).isoformat()
    entry["revive_month"] = current_month

    global _streak_dirty
    _streak_dirty = True
    save_streak_db()
    await backup_streak_db_to_log_channel()
    await _sync_streak_roles(interaction.user.id, entry["streak_days"], entry["phase"])

    ts = int(datetime.fromisoformat(entry["phase_end"]).timestamp())
    day_word = "Day" if entry["streak_days"] == 1 else "Days"
    await interaction.response.send_message(
        f"✅ Revived! Your **{entry['streak_days']} {day_word}** streak is back — "
        f"send {STREAK_MESSAGES_REQUIRED} messages before <t:{ts}:R> to keep it going."
    )


# ---------- Leaderboard image card (ported from leaderboard.js) ----------
# Per-user cooldown, mirroring the JS version's cooldowns Map.
_leaderboard_cooldowns: dict[int, float] = {}
_overall_leaderboard_cooldowns: dict[int, float] = {}


def _leaderboard_rounded_rect(draw: "ImageDraw.ImageDraw", box, radius, **kwargs) -> None:
    draw.rounded_rectangle(box, radius=radius, **kwargs)


def _leaderboard_blend_shape(base: "Image.Image", draw_fn) -> None:
    """Draws onto a fresh transparent layer then alpha-composites it onto `base`, so
    semi-transparent fills blend with what's already been drawn — matching how the
    HTML canvas version layers its rgba() fills on top of each other."""
    overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
    draw_fn(ImageDraw.Draw(overlay))
    base.alpha_composite(overlay)


_leaderboard_font_warned_paths: set = set()


def _leaderboard_load_font(path: str, size: int) -> "ImageFont.FreeTypeFont":
    try:
        return ImageFont.truetype(path, size)
    except OSError as e:
        if path not in _leaderboard_font_warned_paths:
            _leaderboard_font_warned_paths.add(path)
            print(f"[leaderboard] Failed to load font at '{path}': {e!r} — falling back to Pillow's default font.", flush=True)
        return ImageFont.load_default()


def _leaderboard_log_asset_status() -> None:
    """Prints whether each leaderboard asset was actually found on disk at startup,
    so a wrong path or a missing/un-committed file shows up immediately in the Railway
    logs instead of silently rendering with the wrong font."""
    for label, path in (
        ("Orbitron regular/bold font", LEADERBOARD_FONT_PATH),
        ("Orbitron extra-bold font", LEADERBOARD_FONT_EXTRABOLD_PATH),
        ("Background image", LEADERBOARD_BG_PATH),
    ):
        if os.path.exists(path):
            print(f"[leaderboard] Found {label} at '{path}'.", flush=True)
        else:
            print(f"[leaderboard] MISSING {label} — expected at '{path}'.", flush=True)


async def _resolve_leaderboard_username(guild: discord.Guild | None, user_id: int) -> str:
    """Best-effort account username (not server nickname) for the card — checks the
    cached member first, then falls back to a fetch, then to the raw ID if the user
    can't be resolved at all."""
    member = guild.get_member(user_id) if guild else None
    if member:
        return member.name
    user = bot.get_user(user_id)
    if user:
        return user.name
    try:
        user = await bot.fetch_user(user_id)
        return user.name
    except discord.HTTPException:
        return f"User {user_id}"


def _render_leaderboard_image(
    ranked: list[tuple[int, int, str]], current_user_id: int, title: str = "Weekly Leaders"
) -> bytes:
    """Draws the two-column leaderboard card. `ranked` is a list of
    (user_id, message_count, username) tuples, already sorted/sliced to the top 10.
    Runs synchronously — call via asyncio.to_thread, this is not async-safe on its own."""
    W, H = LEADERBOARD_CANVAS_WIDTH, LEADERBOARD_CANVAS_HEIGHT

    try:
        bg = Image.open(LEADERBOARD_BG_PATH).convert("RGBA")
    except (FileNotFoundError, OSError):
        try:
            bg = Image.open(LEADERBOARD_BG_FALLBACK_PATH).convert("RGBA")
        except (FileNotFoundError, OSError):
            bg = Image.new("RGBA", (W, H), (24, 24, 28, 255))
    bg = bg.resize((W, H))

    canvas = bg.copy()

    # Dark overlay card
    _leaderboard_blend_shape(
        canvas,
        lambda d: _leaderboard_rounded_rect(d, (35, 30, 35 + 830, 30 + 460), 14, fill=(0, 0, 0, 179)),
    )

    draw = ImageDraw.Draw(canvas)

    font_title = _leaderboard_load_font(LEADERBOARD_FONT_EXTRABOLD_PATH, 36)
    font_rank = _leaderboard_load_font(LEADERBOARD_FONT_EXTRABOLD_PATH, 16)
    font_msgs = _leaderboard_load_font(LEADERBOARD_FONT_PATH, 16)

    # Title
    draw.text((60, 85), title, font=font_title, fill=(255, 255, 255, 255), anchor="ls")

    # Separator line under the title
    _leaderboard_blend_shape(
        canvas,
        lambda d: d.rectangle((60, 100, 60 + 780, 100 + 2), fill=(255, 255, 255, 38)),
    )
    draw = ImageDraw.Draw(canvas)

    left_column = ranked[0:5]
    right_column = ranked[5:10]

    start_y = 120
    row_height = 56
    row_gap = 18

    def draw_row(user_id: int, count: int, username: str, col_x: int, row_index: int, real_rank: int) -> None:
        row_y = start_y + row_index * (row_height + row_gap)
        is_current_user = user_id == current_user_id

        # Row background
        if is_current_user:
            _leaderboard_blend_shape(
                canvas,
                lambda d: _leaderboard_rounded_rect(
                    d, (col_x, row_y, col_x + 360, row_y + row_height), 10, fill=(88, 101, 242, 64)
                ),
            )
            _leaderboard_blend_shape(
                canvas,
                lambda d: _leaderboard_rounded_rect(
                    d, (col_x, row_y, col_x + 360, row_y + row_height), 10, outline=(88, 101, 242, 127), width=2
                ),
            )
        else:
            _leaderboard_blend_shape(
                canvas,
                lambda d: _leaderboard_rounded_rect(
                    d, (col_x, row_y, col_x + 360, row_y + row_height), 10, fill=(255, 255, 255, 18)
                ),
            )

        row_draw = ImageDraw.Draw(canvas)

        # Rank circle (fully opaque — safe to draw straight onto the canvas)
        rank_circle_x = col_x + 14
        rank_circle_y = row_y + 10
        radius = 18
        row_draw.ellipse(
            (rank_circle_x, rank_circle_y, rank_circle_x + radius * 2, rank_circle_y + radius * 2),
            fill=(30, 30, 30, 255),
            outline=(47, 47, 47, 255),
            width=2,
        )

        if real_rank == 1:
            rank_color = (255, 217, 102, 255)
        elif real_rank == 2:
            rank_color = (207, 207, 207, 255)
        elif real_rank == 3:
            rank_color = (200, 155, 60, 255)
        else:
            rank_color = (255, 255, 255, 255)

        row_draw.text(
            (rank_circle_x + radius, rank_circle_y + radius),
            str(real_rank),
            font=font_rank,
            fill=rank_color,
            anchor="mm",
        )

        # Message count, right-aligned
        msg_text = f"{count} msgs"
        row_draw.text(
            (col_x + 345, row_y + row_height / 2),
            msg_text,
            font=font_msgs,
            fill=(204, 204, 204, 255),
            anchor="rm",
        )
        msg_width = font_msgs.getlength(msg_text)
        max_name_width = (345 - msg_width - 15) - 64

        # Shrink-to-fit username
        font_size = 24
        name_font = _leaderboard_load_font(LEADERBOARD_FONT_PATH, font_size)
        while name_font.getlength(username) > max_name_width and font_size > 12:
            font_size -= 1
            name_font = _leaderboard_load_font(LEADERBOARD_FONT_PATH, font_size)

        row_draw.text(
            (col_x + 64, row_y + row_height / 2),
            username,
            font=name_font,
            fill=(255, 255, 255, 255),
            anchor="lm",
        )

    for i, (user_id, count, username) in enumerate(left_column):
        draw_row(user_id, count, username, 60, i, i + 1)
    for i, (user_id, count, username) in enumerate(right_column):
        draw_row(user_id, count, username, 460, i, i + 6)

    buf = io.BytesIO()
    canvas.convert("RGB").save(buf, format="PNG")
    return buf.getvalue()


@bot.tree.command(name="messageleaderboard", description="Show the most active members by message count")
@app_commands.describe(select="Weekly (resets Sundays) or all-time — defaults to Weekly")
async def messageleaderboard(interaction: discord.Interaction, select: Literal["Weekly", "Overall"] = "Weekly"):
    is_overall = select == "Overall"
    cooldowns = _overall_leaderboard_cooldowns if is_overall else _leaderboard_cooldowns

    # 1. Check cooldown (mirrors leaderboard.js)
    user_id = interaction.user.id
    last_used = cooldowns.get(user_id)
    if last_used is not None:
        expiration_time = last_used + LEADERBOARD_COOLDOWN_SECONDS
        now_ts = time.time()
        if now_ts < expiration_time:
            time_left = round(expiration_time - now_ts)
            await interaction.response.send_message(
                f"⏳ Please wait **{time_left} seconds** before checking the leaderboard again!",
                ephemeral=True,
            )
            return

    # 2. Set cooldown
    cooldowns[user_id] = time.time()

    # 3. Start command
    await interaction.response.defer()

    counts_source = _overall_message_counts if is_overall else _message_leaderboard_counts
    ranked_counts = sorted(counts_source.items(), key=lambda kv: kv[1], reverse=True)[:10]
    if not ranked_counts:
        await interaction.followup.send("No messages logged yet!" if is_overall else "No messages logged this week yet!")
        return

    ranked = [
        (uid, count, await _resolve_leaderboard_username(interaction.guild, uid))
        for uid, count in ranked_counts
    ]

    title = "All-Time Leaders" if is_overall else "Weekly Leaders"
    try:
        image_bytes = await asyncio.to_thread(_render_leaderboard_image, ranked, user_id, title)
    except Exception as e:  # noqa: BLE001 - don't let a bad render kill the command
        print(f"Failed to render leaderboard image: {e!r}")
        await interaction.followup.send("Something went wrong generating the leaderboard image.")
        return

    file = discord.File(io.BytesIO(image_bytes), filename="leaderboard.png")
    await interaction.followup.send(
        content=("🏆 **Here are the all-time top 10 chatters!** 🏆" if is_overall else "🏆 **Here are the current top 10 chatters!** 🏆"),
        file=file,
    )


@bot.tree.command(
    name="syncmessages",
    description="(Staff) Rebuild this week's message counts by rescanning channel history since the last reset",
)
async def syncmessages(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    if not has_staff_role(interaction.user):
        await interaction.followup.send("You don't have permission to use this command.", ephemeral=True)
        return

    # Only the designated tracking channel counts now — mirrors what on_message does live,
    # so a resync can't reintroduce counts from every other channel in the server.
    guild = interaction.guild
    if guild is None:
        await interaction.followup.send("This command can only be used in a server.", ephemeral=True)
        return

    tracked_channel = guild.get_channel(MESSAGE_LEADERBOARD_TRACKED_CHANNEL_ID)
    if tracked_channel is None:
        await interaction.followup.send(
            "Couldn't find the tracked channel — check MESSAGE_LEADERBOARD_TRACKED_CHANNEL_ID.",
            ephemeral=True,
        )
        return

    # The boundary is this week's currently-in-effect Sunday-23:59-UTC reset — i.e. "last
    # Sunday 23:59" relative to right now, same boundary record_message_for_leaderboard uses.
    boundary = _message_leaderboard_reset_boundary(discord.utils.utcnow())

    channels_to_scan: list = [tracked_channel]
    channels_to_scan.extend(getattr(tracked_channel, "threads", []))
    try:
        async for thread in tracked_channel.archived_threads(limit=None):
            channels_to_scan.append(thread)
    except (discord.Forbidden, discord.HTTPException, AttributeError):
        pass

    counts: dict[int, int] = {}
    last_counted_at: dict[int, datetime] = {}  # mirrors the live 5s-per-user spam gate
    scanned_channels = 0
    skipped_channels = 0
    total_messages = 0

    for channel in channels_to_scan:
        try:
            async for message in channel.history(after=boundary, limit=None, oldest_first=True):
                if message.author.bot:
                    continue
                last = last_counted_at.get(message.author.id)
                if last is not None and (message.created_at - last).total_seconds() < MESSAGE_COUNT_SPAM_COOLDOWN_SECONDS:
                    continue
                last_counted_at[message.author.id] = message.created_at
                counts[message.author.id] = counts.get(message.author.id, 0) + 1
                total_messages += 1
            scanned_channels += 1
        except (discord.Forbidden, discord.HTTPException):
            skipped_channels += 1
            continue

    global _message_leaderboard_period_start, _message_leaderboard_dirty
    _message_leaderboard_counts.clear()
    _message_leaderboard_counts.update(counts)
    _message_leaderboard_period_start = boundary
    _message_leaderboard_dirty = True
    save_message_leaderboard_db()
    await backup_message_leaderboard_to_log_channel()

    await interaction.followup.send(
        f"✅ Resynced weekly message counts from <t:{int(boundary.timestamp())}:f> — "
        f"scanned {scanned_channels} channel(s)/thread(s) ({skipped_channels} skipped due to missing access), "
        f"counted {total_messages:,} messages across {len(counts)} member(s).",
        ephemeral=True,
    )


@bot.tree.command(
    name="changemessagetracking",
    description="(Staff) Manually add or remove weekly tracked messages for a user",
)
@app_commands.describe(
    user="The member whose weekly message count to adjust",
    add="Number of messages to add to their count",
    remove="Number of messages to remove from their count",
)
async def changemessagetracking(
    interaction: discord.Interaction,
    user: discord.Member | None = None,
    add: app_commands.Range[int, 1, None] | None = None,
    remove: app_commands.Range[int, 1, None] | None = None,
):
    if not isinstance(interaction.user, discord.Member) or not has_staff_role(interaction.user):
        await interaction.response.send_message("You don't have permission to use this command.", ephemeral=True)
        return

    if user is None:
        await interaction.response.send_message("Please specify a `user`.", ephemeral=True)
        return

    if (add is None) == (remove is None):  # neither given, or both given
        await interaction.response.send_message(
            "Please provide exactly one of `add` or `remove`.", ephemeral=True
        )
        return

    global _message_leaderboard_dirty
    current = _message_leaderboard_counts.get(user.id, 0)
    if add is not None:
        new_count = current + add
        action_desc = f"Added **{add}**"
    else:
        new_count = max(0, current - remove)
        action_desc = f"Removed **{remove}**"

    _message_leaderboard_counts[user.id] = new_count
    _message_leaderboard_dirty = True
    save_message_leaderboard_db()
    await backup_message_leaderboard_to_log_channel()

    await interaction.response.send_message(
        f"{action_desc} tracked message(s) for {user.mention}. New weekly count: **{new_count}**."
    )


# ---------- Personal message-rank card (/messages) ----------
RANK_CARD_CANVAS_WIDTH = 1000
RANK_CARD_CANVAS_HEIGHT = 600


async def _fetch_leaderboard_avatar_bytes(user_or_member) -> bytes | None:
    try:
        asset = user_or_member.display_avatar.replace(size=256, format="png")
        return await asset.read()
    except (discord.HTTPException, discord.NotFound):
        return None


def _circular_avatar(avatar_bytes: bytes | None, diameter: int) -> "Image.Image":
    if avatar_bytes:
        try:
            img = Image.open(io.BytesIO(avatar_bytes)).convert("RGBA")
            img = img.resize((diameter, diameter))
        except OSError:
            img = None
    else:
        img = None

    if img is None:
        # Plain placeholder circle if the avatar couldn't be loaded.
        img = Image.new("RGBA", (diameter, diameter), (60, 60, 68, 255))

    mask = Image.new("L", (diameter, diameter), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, diameter, diameter), fill=255)
    out = Image.new("RGBA", (diameter, diameter), (0, 0, 0, 0))
    out.paste(img, (0, 0), mask)
    return out


def _rank_tier_color(rank: int) -> tuple:
    if rank == 1:
        return (255, 217, 102, 255)
    if rank == 2:
        return (207, 207, 207, 255)
    if rank == 3:
        return (200, 155, 60, 255)
    return (94, 224, 236, 255)  # cyan — everyone outside the top 3


def _render_message_rank_card(
    target: dict,
    neighbor_above: dict | None,
    neighbor_below: dict | None,
    avatar_bytes: bytes | None,
    stat_label: str = "Messages This Week",
    rank_label: str = "Weekly Rank",
    top_rank_text: str = "You're #1 this week! 🏆",
) -> bytes:
    """target/neighbor_above/neighbor_below are dicts: {"rank", "username", "count"}."""
    W, H = RANK_CARD_CANVAS_WIDTH, RANK_CARD_CANVAS_HEIGHT

    try:
        bg = Image.open(LEADERBOARD_BG_PATH).convert("RGBA")
    except (FileNotFoundError, OSError):
        try:
            bg = Image.open(LEADERBOARD_BG_FALLBACK_PATH).convert("RGBA")
        except (FileNotFoundError, OSError):
            bg = Image.new("RGBA", (W, H), (24, 24, 28, 255))
    canvas = bg.resize((W, H)).copy()

    # Outer translucent panel
    panel_box = (40, 40, W - 40, H - 40)
    _leaderboard_blend_shape(
        canvas,
        lambda d: (
            d.rounded_rectangle(panel_box, radius=22, fill=(0, 0, 0, 140)),
            d.rounded_rectangle(panel_box, radius=22, outline=(255, 255, 255, 40), width=2),
        ),
    )

    top_y0, top_y1 = 40, 150
    mid_y0, mid_y1 = 150, 450
    bot_y0, bot_y1 = 450, 560

    _leaderboard_blend_shape(
        canvas,
        lambda d: (
            d.rectangle((60, top_y1, W - 60, top_y1 + 2), fill=(255, 255, 255, 30)),
            d.rectangle((60, mid_y1, W - 60, mid_y1 + 2), fill=(255, 255, 255, 30)),
        ),
    )

    draw = ImageDraw.Draw(canvas)
    font_neighbor_rank = _leaderboard_load_font(LEADERBOARD_FONT_EXTRABOLD_PATH, 26)
    font_neighbor_name = _leaderboard_load_font(LEADERBOARD_FONT_PATH, 22)
    font_neighbor_msgs = _leaderboard_load_font(LEADERBOARD_FONT_PATH, 20)

    def draw_neighbor_row(neighbor: dict | None, y0: int, y1: int, empty_text: str) -> None:
        cy = (y0 + y1) / 2
        if neighbor is None:
            draw.text((70, cy), empty_text, font=font_neighbor_name, fill=(150, 150, 158, 255), anchor="lm")
            return
        draw.text(
            (70, cy),
            f"#{neighbor['rank']}",
            font=font_neighbor_rank,
            fill=_rank_tier_color(neighbor["rank"]),
            anchor="lm",
        )
        draw.text((160, cy), neighbor["username"], font=font_neighbor_name, fill=(230, 230, 230, 255), anchor="lm")
        draw.text(
            (W - 70, cy),
            f"{neighbor['count']} msgs",
            font=font_neighbor_msgs,
            fill=(255, 217, 102, 255),
            anchor="rm",
        )

    draw_neighbor_row(neighbor_above, top_y0, top_y1, top_rank_text)
    draw_neighbor_row(neighbor_below, bot_y0, bot_y1, "Nobody's ranked below you yet.")

    # Avatar
    avatar_diameter = 180
    avatar_x, avatar_y = 90, int((mid_y0 + mid_y1) / 2 - avatar_diameter / 2)
    avatar_img = _circular_avatar(avatar_bytes, avatar_diameter)
    canvas.paste(avatar_img, (avatar_x, avatar_y), avatar_img)
    ring_color = _rank_tier_color(target["rank"])
    draw = ImageDraw.Draw(canvas)
    draw.ellipse(
        (avatar_x - 2, avatar_y - 2, avatar_x + avatar_diameter + 2, avatar_y + avatar_diameter + 2),
        outline=ring_color,
        width=4,
    )

    text_x = avatar_x + avatar_diameter + 40
    username_y = mid_y0 + 65

    font_username = _leaderboard_load_font(LEADERBOARD_FONT_EXTRABOLD_PATH, 42)
    max_username_width = (W - 70) - text_x
    username_font_size = 42
    while font_username.getlength(target["username"]) > max_username_width and username_font_size > 20:
        username_font_size -= 2
        font_username = _leaderboard_load_font(LEADERBOARD_FONT_EXTRABOLD_PATH, username_font_size)
    draw.text((text_x, username_y), target["username"], font=font_username, fill=(255, 255, 255, 255), anchor="lm")

    font_label = _leaderboard_load_font(LEADERBOARD_FONT_PATH, 20)
    font_stat = _leaderboard_load_font(LEADERBOARD_FONT_EXTRABOLD_PATH, 56)

    label_y = username_y + 55
    stat_y = label_y + 55

    col1_x = text_x
    col2_x = text_x + 350

    draw.text((col1_x, label_y), stat_label, font=font_label, fill=(200, 200, 200, 255), anchor="lm")
    draw.text((col1_x, stat_y), str(target["count"]), font=font_stat, fill=(255, 217, 102, 255), anchor="lm")

    draw.text((col2_x, label_y), rank_label, font=font_label, fill=(200, 200, 200, 255), anchor="lm")
    draw.text((col2_x, stat_y), f"#{target['rank']}", font=font_stat, fill=ring_color, anchor="lm")

    buf = io.BytesIO()
    canvas.convert("RGB").save(buf, format="PNG")
    return buf.getvalue()


# ---------- Streak card (/streakcount) ----------
STREAK_CARD_CANVAS_WIDTH = 1100
STREAK_CARD_CANVAS_HEIGHT = 320
STREAK_CARD_ACCENT_COLOR = (255, 122, 26, 255)  # 🔥 orange — streak avatar ring + streak number


def _render_streak_card(username: str, avatar_bytes: bytes | None, streak_days: int, rank: int | None) -> bytes:
    """Wide banner card: avatar on the left, username above two stat columns
    (Current Streak / Global Rank) on the right. Runs synchronously — call via
    asyncio.to_thread, same as the other leaderboard/rank cards."""
    W, H = STREAK_CARD_CANVAS_WIDTH, STREAK_CARD_CANVAS_HEIGHT

    try:
        bg = Image.open(LEADERBOARD_BG_PATH).convert("RGBA")
    except (FileNotFoundError, OSError):
        try:
            bg = Image.open(LEADERBOARD_BG_FALLBACK_PATH).convert("RGBA")
        except (FileNotFoundError, OSError):
            bg = Image.new("RGBA", (W, H), (24, 24, 28, 255))
    canvas = bg.resize((W, H)).copy()

    panel_box = (30, 30, W - 30, H - 30)
    _leaderboard_blend_shape(
        canvas,
        lambda d: (
            d.rounded_rectangle(panel_box, radius=24, fill=(0, 0, 0, 140)),
            d.rounded_rectangle(panel_box, radius=24, outline=(255, 255, 255, 40), width=2),
        ),
    )
    draw = ImageDraw.Draw(canvas)

    avatar_diameter = 180
    avatar_x = 70
    avatar_y = (H - avatar_diameter) // 2
    avatar_img = _circular_avatar(avatar_bytes, avatar_diameter)
    canvas.paste(avatar_img, (avatar_x, avatar_y), avatar_img)
    draw = ImageDraw.Draw(canvas)
    draw.ellipse(
        (avatar_x - 3, avatar_y - 3, avatar_x + avatar_diameter + 3, avatar_y + avatar_diameter + 3),
        outline=STREAK_CARD_ACCENT_COLOR,
        width=5,
    )

    text_x = avatar_x + avatar_diameter + 55
    username_y = 95

    font_username = _leaderboard_load_font(LEADERBOARD_FONT_EXTRABOLD_PATH, 44)
    max_username_width = (W - 60) - text_x
    username_font_size = 44
    while font_username.getlength(username) > max_username_width and username_font_size > 22:
        username_font_size -= 2
        font_username = _leaderboard_load_font(LEADERBOARD_FONT_EXTRABOLD_PATH, username_font_size)
    draw.text((text_x, username_y), username, font=font_username, fill=(255, 255, 255, 255), anchor="lm")

    label_y = username_y + 75
    stat_y = label_y + 60

    col1_x = text_x
    col2_x = text_x + 330

    font_label = _leaderboard_load_font(LEADERBOARD_FONT_PATH, 24)
    font_stat = _leaderboard_load_font(LEADERBOARD_FONT_EXTRABOLD_PATH, 52)

    day_word = "Day" if streak_days == 1 else "Days"
    rank_text = f"#{rank}" if rank is not None else "—"

    draw.text((col1_x, label_y), "Current Streak", font=font_label, fill=(200, 200, 200, 255), anchor="lm")
    draw.text(
        (col1_x, stat_y), f"{streak_days} {day_word}", font=font_stat, fill=STREAK_CARD_ACCENT_COLOR, anchor="lm"
    )

    draw.text((col2_x, label_y), "Global Rank", font=font_label, fill=(200, 200, 200, 255), anchor="lm")
    draw.text((col2_x, stat_y), rank_text, font=font_stat, fill=(94, 224, 236, 255), anchor="lm")

    buf = io.BytesIO()
    canvas.convert("RGB").save(buf, format="PNG")
    return buf.getvalue()


@bot.tree.command(name="streakcount", description="Show your (or someone else's) chat streak")
@app_commands.describe(user="Whose streak to show — defaults to you")
async def streakcount(interaction: discord.Interaction, user: discord.Member | None = None):
    await interaction.response.defer()

    target = user or interaction.user
    entry = _streak_data.get(target.id)
    streak_days = entry["streak_days"] if entry else 0

    if streak_days <= 0:
        who = "You don't" if target.id == interaction.user.id else f"**{target.display_name}** doesn't"
        await interaction.followup.send(f"{who} have an active streak right now — send some messages to start one!")
        return

    ranked = sorted(
        ((uid, e["streak_days"]) for uid, e in _streak_data.items() if e["streak_days"] > 0),
        key=lambda kv: kv[1],
        reverse=True,
    )
    rank = next((i + 1 for i, (uid, _days) in enumerate(ranked) if uid == target.id), None)

    avatar_bytes = await _fetch_leaderboard_avatar_bytes(target)

    try:
        image_bytes = await asyncio.to_thread(_render_streak_card, target.name, avatar_bytes, streak_days, rank)
    except Exception as e:  # noqa: BLE001 - don't let a bad render kill the command
        print(f"Failed to render streak card: {e!r}")
        await interaction.followup.send("Something went wrong generating that card.")
        return

    file = discord.File(io.BytesIO(image_bytes), filename="streak.png")
    await interaction.followup.send(file=file)


@bot.tree.command(name="messages", description="Show your (or someone else's) message stats")
@app_commands.describe(
    select="Weekly (resets Sundays) or all-time — defaults to Weekly",
    user="Whose stats to show — defaults to you",
)
async def messages_command(
    interaction: discord.Interaction,
    select: Literal["Weekly", "Overall"] = "Weekly",
    user: discord.Member | None = None,
):
    await interaction.response.defer()

    is_overall = select == "Overall"
    counts_source = _overall_message_counts if is_overall else _message_leaderboard_counts

    target_member = user or interaction.user
    target_id = target_member.id

    ranked = sorted(counts_source.items(), key=lambda kv: kv[1], reverse=True)
    positions = {uid: i for i, (uid, _count) in enumerate(ranked)}

    if target_id not in positions:
        who = "You haven't" if target_member.id == interaction.user.id else f"**{target_member.display_name}** hasn't"
        await interaction.followup.send(f"{who} sent any tracked messages yet!" if is_overall else f"{who} sent any tracked messages this week yet!")
        return

    idx = positions[target_id]

    async def build_entry(i: int) -> dict:
        uid, count = ranked[i]
        return {"rank": i + 1, "username": await _resolve_leaderboard_username(interaction.guild, uid), "count": count}

    target_entry = await build_entry(idx)
    neighbor_above = await build_entry(idx - 1) if idx > 0 else None
    neighbor_below = await build_entry(idx + 1) if idx < len(ranked) - 1 else None

    avatar_bytes = await _fetch_leaderboard_avatar_bytes(target_member)

    try:
        if is_overall:
            image_bytes = await asyncio.to_thread(
                _render_message_rank_card,
                target_entry, neighbor_above, neighbor_below, avatar_bytes,
                stat_label="Total Messages", rank_label="All-Time Rank", top_rank_text="You're #1 all-time! 🏆",
            )
        else:
            image_bytes = await asyncio.to_thread(
                _render_message_rank_card, target_entry, neighbor_above, neighbor_below, avatar_bytes
            )
    except Exception as e:  # noqa: BLE001 - don't let a bad render kill the command
        print(f"Failed to render message rank card: {e!r}")
        await interaction.followup.send("Something went wrong generating that card.")
        return

    file = discord.File(io.BytesIO(image_bytes), filename="messages.png")
    await interaction.followup.send(file=file)


@bot.tree.command(
    name="syncglobalmessages",
    description="(Staff) Rebuild all-time message counts by rescanning as much of the server's history as possible",
)
async def syncglobalmessages(interaction: discord.Interaction):
    if not isinstance(interaction.user, discord.Member) or not has_staff_role(interaction.user):
        await interaction.response.send_message("You don't have permission to use this command.", ephemeral=True)
        return

    guild = interaction.guild
    if guild is None:
        await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
        return

    await interaction.response.defer()

    # Every channel that can hold messages: text channels, forum posts, and both active +
    # archived threads off of them — as much of the server's history as the API exposes.
    channels_to_scan: list = list(guild.text_channels)
    for text_channel in guild.text_channels:
        channels_to_scan.extend(text_channel.threads)
        try:
            async for thread in text_channel.archived_threads(limit=None):
                channels_to_scan.append(thread)
        except (discord.Forbidden, discord.HTTPException):
            pass
    for forum in getattr(guild, "forums", []):
        channels_to_scan.extend(forum.threads)
        try:
            async for thread in forum.archived_threads(limit=None):
                channels_to_scan.append(thread)
        except (discord.Forbidden, discord.HTTPException):
            pass

    total_channels = len(channels_to_scan)
    # Progress is posted as a normal channel message rather than an interaction followup edit
    # — the interaction token only stays valid 15 minutes, and scanning full history across
    # every channel in an active server can easily take far longer than that.
    await interaction.followup.send(
        f"Starting a full-history resync across {total_channels} channel(s)/thread(s) — progress below."
    )
    progress_msg = await interaction.channel.send(f"Scanning 0/{total_channels} channel(s)...")

    counts: dict[int, int] = {}
    last_counted_at: dict[int, datetime] = {}  # mirrors the live 5s-per-user spam gate
    scanned_channels = 0
    skipped_channels = 0
    total_messages = 0

    for channel in channels_to_scan:
        try:
            async for message in channel.history(limit=None, oldest_first=True):
                if message.author.bot:
                    continue
                last = last_counted_at.get(message.author.id)
                if last is not None and (message.created_at - last).total_seconds() < MESSAGE_COUNT_SPAM_COOLDOWN_SECONDS:
                    continue
                last_counted_at[message.author.id] = message.created_at
                counts[message.author.id] = counts.get(message.author.id, 0) + 1
                total_messages += 1
            scanned_channels += 1
        except (discord.Forbidden, discord.HTTPException):
            skipped_channels += 1
            continue
        except Exception as e:
            print(f"syncglobalmessages: error scanning channel {getattr(channel, 'id', '?')}: {e!r}")
            skipped_channels += 1
            continue

        try:
            await progress_msg.edit(
                content=(
                    f"Scanning {scanned_channels + skipped_channels}/{total_channels} channel(s)... "
                    f"{total_messages:,} messages counted so far."
                )
            )
        except Exception as e:
            print(f"syncglobalmessages: progress edit failed: {e!r}")

    global _overall_message_dirty
    _overall_message_counts.clear()
    _overall_message_counts.update(counts)
    _overall_message_dirty = True
    save_overall_message_db()
    await backup_overall_message_to_log_channel()

    await progress_msg.edit(
        content=(
            "✅ All-time message resync complete.\n"
            f"Scanned {scanned_channels} channel(s)/thread(s) ({skipped_channels} skipped due to missing access).\n"
            f"Counted {total_messages:,} messages across {len(counts)} member(s)."
        )
    )


# ---------- Slash commands ----------
@bot.tree.command(name="createteam", description="Create a new team")
@app_commands.describe(
    name="Team name",
    emoji="A single standard Discord emoji for the team (no custom server emojis)",
    colour="Hex colour for the team's role, e.g. #5865F2",
)
async def createteam(interaction: discord.Interaction, name: str, emoji: str, colour: str):
    await interaction.response.defer(ephemeral=True)

    if not is_valid_standard_emoji(emoji):
        await interaction.followup.send(
            "That's not a standard Discord emoji. Please use a single regular emoji "
            "(custom server emojis can't be used in channel names or role icons).",
            ephemeral=True,
        )
        return

    normalized_colour = normalize_hex_colour(colour)
    if normalized_colour is None:
        await interaction.followup.send(
            "That's not a valid hex colour. Use a format like `#5865F2`.", ephemeral=True
        )
        return

    db = load_db()

    if find_team_key_ci(db["teams"], name):
        await interaction.followup.send(
            f"A team called **{name}** already exists. Pick a different name.", ephemeral=True
        )
        return

    existing = find_team_by_leader(db["teams"], interaction.user.id)
    if existing:
        view = DeleteTeamView(interaction.user.id, existing, interaction.guild)
        await interaction.followup.send(
            f"You already lead a team called **{existing}**. You can only lead one team at a time.",
            view=view,
            ephemeral=True,
        )
        return

    existing_membership = find_team_by_member(db["teams"], interaction.user.id)
    if existing_membership:
        await interaction.followup.send(
            f"You're already a member of **{existing_membership}**. Leave that team with "
            f"`/leaveteam` before creating a new one.",
            ephemeral=True,
        )
        return

    if interaction.user.id in pending_team_requests:
        await interaction.followup.send(
            "You already have a team creation request awaiting admin confirmation. "
            "Please wait for that to be approved or denied before submitting another.",
            ephemeral=True,
        )
        return

    pending_team_requests.add(interaction.user.id)

    confirm_channel = bot.get_channel(CONFIRM_CHANNEL_ID) or await bot.fetch_channel(CONFIRM_CHANNEL_ID)
    view = ConfirmTeamView(
        requester_id=interaction.user.id,
        team_name=name,
        emoji=emoji,
        colour=normalized_colour,
        guild=interaction.guild,
    )
    sent = await confirm_channel.send(
        content=f"{interaction.user.mention} wants to create team **{name}** {emoji}. Admins, confirm?",
        view=view,
    )
    view.message = sent
    await interaction.followup.send(
        f"Sent to {confirm_channel.mention} for admin confirmation ✅", ephemeral=True
    )


class TeamMembersView(discord.ui.LayoutView):
    """A Components V2 container listing a team's members, styled to match
    MetaUpdateView/MemberCountView elsewhere in the bot — accent-bordered card
    instead of a plain embed."""

    def __init__(self, key: str, info: dict, role: discord.Role):
        super().__init__(timeout=None)

        members = sorted(role.members, key=lambda m: m.id != info["leader_id"])
        lines = [
            member.mention + (" (Leader)" if member.id == info["leader_id"] else "")
            for member in members
        ]

        children = [
            discord.ui.TextDisplay(f"# {info['emoji']} {key} Team"),
            discord.ui.Separator(),
            discord.ui.TextDisplay("\n".join(lines) if lines else "No members with this role yet."),
        ]

        container = discord.ui.Container(*children)
        self.add_item(container)


@bot.tree.command(name="teammembers", description="List a team's members")
@app_commands.describe(team="Team name")
async def teammembers(interaction: discord.Interaction, team: str):
    await interaction.response.defer()

    db = load_db()
    key = find_team_key_ci(db["teams"], team)
    if not key:
        await interaction.followup.send("No team found with that name.", ephemeral=True)
        return

    info = db["teams"][key]
    role = interaction.guild.get_role(info["role_id"])
    if role is None:
        await interaction.followup.send("That team's role no longer exists.", ephemeral=True)
        return

    await interaction.followup.send(view=TeamMembersView(key, info, role))


@teammembers.autocomplete("team")
async def teammembers_team_autocomplete(interaction: discord.Interaction, current: str):
    db = load_db()
    return [
        app_commands.Choice(name=key, value=key)
        for key in db["teams"].keys()
        if current.lower() in key.lower()
    ][:25]


@bot.tree.command(name="invite", description="Invite a user to your team (leader or co-leader)")
@app_commands.describe(user="The user to invite")
async def invite(interaction: discord.Interaction, user: discord.Member):
    await interaction.response.defer(ephemeral=True)

    db = load_db()
    team_key = find_team_by_leader_or_coleader(db["teams"], interaction.user.id)
    if not team_key:
        await interaction.followup.send("You must be a team leader or co-leader to invite people.", ephemeral=True)
        return

    # Only one outstanding invite per leader at a time — if they already have one pending,
    # give them a way to cancel it instead of letting them queue up another.
    if interaction.user.id in pending_invites:
        await interaction.followup.send(
            "You already have a pending invite. Click the red button below to cancel.",
            view=CancelInviteView(interaction.user.id),
            ephemeral=True,
        )
        return

    if user.bot:
        await interaction.followup.send("You can't invite bots.", ephemeral=True)
        return

    if find_team_by_member(db["teams"], user.id):
        await interaction.followup.send("That user is already on a team.", ephemeral=True)
        return

    info = db["teams"][team_key]

    if not info.get("bypass_member_limit", False) and len(info.get("members", [])) >= MAX_TEAM_MEMBERS:
        await interaction.followup.send(
            f"**{team_key}** is already at the {MAX_TEAM_MEMBERS}-member cap — remove someone first.",
            ephemeral=True,
        )
        return

    view = InviteResponseView(team_key, user.id, interaction.guild.id, interaction.user.id)
    try:
        sent = await user.send(
            f"{interaction.user.mention} invited you to join **{team_key}** {info['emoji']}! "
            f"Would you like to join?",
            view=view,
        )
    except discord.Forbidden:
        await log_team_event(
            "⚠️ Invite Failed (DMs Closed)",
            colour=discord.Colour.orange(),
            fields=[
                ("Team", team_key, True),
                ("Invited User", f"{user.mention} (`{user.id}`)", True),
                ("Invited By", f"{interaction.user.mention} (`{interaction.user.id}`)", True),
            ],
        )
        await interaction.followup.send(
            "Couldn't DM that user (they may have DMs off).", ephemeral=True
        )
        return

    await log_team_event(
        "📨 Invite Sent",
        colour=discord.Colour.blue(),
        fields=[
            ("Team", team_key, True),
            ("Invited User", f"{user.mention} (`{user.id}`)", True),
            ("Invited By", f"{interaction.user.mention} (`{interaction.user.id}`)", True),
        ],
    )

    view.message = sent
    pending_invites[interaction.user.id] = {
        "team": team_key,
        "invited_user_id": user.id,
        "dm_channel_id": sent.channel.id,
        "dm_message_id": sent.id,
    }

    await interaction.followup.send(f"Invite sent to {user.mention}.", ephemeral=True)


@bot.tree.command(name="startscrim", description="Challenge another team's leader to a scrim")
@app_commands.describe(team="The team you want to scrim")
async def startscrim(interaction: discord.Interaction, team: str):
    await interaction.response.defer(ephemeral=True)

    db = load_db()
    challenger_team = find_team_by_leader(db["teams"], interaction.user.id)
    if not challenger_team:
        await interaction.followup.send("You must be a team leader to start a scrim.", ephemeral=True)
        return

    opponent_team = find_team_key_ci(db["teams"], team)
    if not opponent_team:
        await interaction.followup.send("No team found with that name.", ephemeral=True)
        return

    if opponent_team == challenger_team:
        await interaction.followup.send("You can't scrim your own team.", ephemeral=True)
        return

    # Only one outstanding scrim request per leader at a time — if they already have one
    # pending, give them a way to cancel it instead of letting them queue up another.
    if interaction.user.id in pending_scrim_requests:
        await interaction.followup.send(
            "You already have a pending scrim request. Click the red button below to cancel.",
            view=CancelScrimRequestView(interaction.user.id),
            ephemeral=True,
        )
        return

    opponent_info = db["teams"][opponent_team]
    opponent_leader_id = opponent_info.get("leader_id")
    if opponent_leader_id is None:
        await interaction.followup.send(f"**{opponent_team}** doesn't have a leader on record.", ephemeral=True)
        return

    opponent_leader = interaction.guild.get_member(opponent_leader_id) or await interaction.guild.fetch_member(opponent_leader_id)

    view = ScrimResponseView(challenger_team, interaction.user.id, opponent_team, opponent_leader_id, interaction.guild.id)
    try:
        sent = await opponent_leader.send(
            f"**{challenger_team}** (challenged by {interaction.user.mention}) wants to scrim your team, "
            f"**{opponent_team}**! Would you like to accept?",
            view=view,
        )
    except discord.Forbidden:
        await log_team_event(
            "⚠️ Scrim Request Failed (DMs Closed)",
            colour=discord.Colour.orange(),
            fields=[
                ("Team A", challenger_team, True),
                ("Team B", opponent_team, True),
                ("Requested By", f"{interaction.user.mention} (`{interaction.user.id}`)", True),
            ],
        )
        await interaction.followup.send(
            f"Couldn't DM {opponent_team}'s leader (they may have DMs off).", ephemeral=True
        )
        return

    await log_team_event(
        "📨 Scrim Request Sent",
        colour=discord.Colour.blue(),
        fields=[
            ("Team A", challenger_team, True),
            ("Team B", opponent_team, True),
            ("Requested By", f"{interaction.user.mention} (`{interaction.user.id}`)", True),
        ],
    )

    view.message = sent
    pending_scrim_requests[interaction.user.id] = {
        "opponent_team": opponent_team,
        "opponent_leader_id": opponent_leader_id,
        "dm_channel_id": sent.channel.id,
        "dm_message_id": sent.id,
    }

    await interaction.followup.send(f"Scrim request sent to {opponent_team}'s leader.", ephemeral=True)


@startscrim.autocomplete("team")
async def startscrim_team_autocomplete(interaction: discord.Interaction, current: str):
    db = load_db()
    own_team = find_team_by_leader(db["teams"], interaction.user.id)
    return [
        app_commands.Choice(name=key, value=key)
        for key in db["teams"].keys()
        if current.lower() in key.lower() and key != own_team
    ][:25]


@bot.tree.command(name="leaveteam", description="Leave your current team")
async def leaveteam(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    db = load_db()
    team_key = find_team_by_member(db["teams"], interaction.user.id)
    if not team_key:
        await interaction.followup.send("You're not in a team.", ephemeral=True)
        return

    info = db["teams"][team_key]
    if interaction.user.id == info["leader_id"]:
        await interaction.followup.send(
            "You're the leader of this team, so you can't leave it. "
            "Use `/changeteamsettings delete:True` if you want to delete it instead.",
            ephemeral=True,
        )
        return

    eligible_ts = get_team_leave_eligible_ts(info, interaction.user.id)
    now_ts = int(discord.utils.utcnow().timestamp())
    if eligible_ts is not None and now_ts < eligible_ts:
        await interaction.followup.send(
            f"You joined **{team_key}** less than {TEAM_JOIN_COOLDOWN_DAYS} days ago, so you "
            f"can't leave (and join a different team) yet. You'll be able to leave "
            f"<t:{eligible_ts}:R> (<t:{eligible_ts}:F>).",
            ephemeral=True,
        )
        return

    role = interaction.guild.get_role(info["role_id"])
    if role:
        await interaction.user.remove_roles(role, reason="Left the team")

    # Same co-leader cleanup as perform_team_kick — see the comment there.
    if info.get("co_leader_id") == interaction.user.id:
        info["co_leader_id"] = None
        channel_for_perms = interaction.guild.get_channel(info.get("channel_id"))
        if channel_for_perms is not None:
            try:
                await channel_for_perms.set_permissions(interaction.user, overwrite=None, reason="Left the team")
            except discord.HTTPException:
                pass
        co_leader_role = interaction.guild.get_role(TEAM_CO_LEADER_ROLE_ID)
        if co_leader_role is not None and co_leader_role in interaction.user.roles:
            try:
                await interaction.user.remove_roles(co_leader_role, reason="Left the team")
            except discord.HTTPException:
                pass

    info["members"] = [uid for uid in info["members"] if uid != interaction.user.id]
    clear_team_join(info, interaction.user.id)
    save_db(db)
    await backup_db_to_log_channel()

    await log_team_event(
        "🚪 Member Left Team",
        colour=discord.Colour.orange(),
        fields=[
            ("Team", team_key, True),
            ("Member", f"{interaction.user.mention} (`{interaction.user.id}`)", True),
        ],
    )

    channel = interaction.guild.get_channel(info["channel_id"])
    if channel is not None:
        try:
            await channel.send(build_team_leave_message(interaction.user.id))
        except discord.HTTPException:
            pass

    await interaction.followup.send(f"You left **{team_key}**.", ephemeral=True)


@bot.tree.command(
    name="forcekick",
    description="(Staff) Remove a member from their team immediately, no leave cooldown applies",
)
@app_commands.describe(member="The member to force-kick from their current team")
async def forcekick(interaction: discord.Interaction, member: discord.Member):
    await interaction.response.defer(ephemeral=True)

    if not has_staff_role(interaction.user):
        await interaction.followup.send("You don't have permission to use this command.", ephemeral=True)
        return

    db = load_db()
    team_key = find_team_by_member(db["teams"], member.id)
    if not team_key:
        await interaction.followup.send(f"{member.mention} isn't on a team.", ephemeral=True)
        return

    info = db["teams"][team_key]
    if member.id == info.get("leader_id"):
        await interaction.followup.send(
            f"{member.mention} leads **{team_key}** — use `/staffchangesetting delete:True` if you "
            f"want to remove the team entirely.",
            ephemeral=True,
        )
        return

    await perform_team_kick(
        db, team_key, member.id, interaction.guild, reason=f"Force-kicked from team by staff member {interaction.user}"
    )

    await interaction.followup.send(f"Force-removed {member.mention} from **{team_key}**.", ephemeral=True)


# Extra user allowed to use /forceadd even without the staff role.
FORCEADD_EXTRA_USER_ID = 1221101672902693005


@bot.tree.command(
    name="forceadd",
    description="(Staff) Force-add a member to a team, bypassing invites, the member cap, and the join cooldown",
)
@app_commands.describe(team="Team to add the member to", user="The member to add")
async def forceadd(interaction: discord.Interaction, team: str, user: discord.Member):
    await interaction.response.defer(ephemeral=True)

    if not (has_staff_role(interaction.user) or interaction.user.id == FORCEADD_EXTRA_USER_ID):
        await interaction.followup.send("You don't have permission to use this command.", ephemeral=True)
        return

    db = load_db()
    team_key = find_team_key_ci(db["teams"], team)
    if not team_key:
        await interaction.followup.send("No team found with that name.", ephemeral=True)
        return

    if user.bot:
        await interaction.followup.send("You can't add bots to a team.", ephemeral=True)
        return

    existing_team = find_team_by_member(db["teams"], user.id)
    if existing_team:
        if existing_team == team_key:
            await interaction.followup.send(f"{user.mention} is already on **{team_key}**.", ephemeral=True)
        else:
            await interaction.followup.send(
                f"{user.mention} is already on **{existing_team}** — use `/forcekick` to remove them "
                f"from it first.",
                ephemeral=True,
            )
        return

    info = db["teams"][team_key]

    role = interaction.guild.get_role(info["role_id"])
    if role:
        await user.add_roles(role, reason=f"Force-added to team by staff member {interaction.user}")

    if user.id not in info["members"]:
        info["members"].append(user.id)
    record_team_join(info, user.id)
    save_db(db)
    await backup_db_to_log_channel()

    await log_team_event(
        "➕ Member Force-Added To Team",
        colour=discord.Colour.green(),
        fields=[
            ("Team", team_key, True),
            ("Member", f"{user.mention} (`{user.id}`)", True),
            ("Added By", f"{interaction.user.mention} (`{interaction.user.id}`)", True),
        ],
    )

    channel = interaction.guild.get_channel(info["channel_id"])
    if channel:
        try:
            await channel.send(f"🎉 {user.mention} was just added to the team!")
        except discord.HTTPException:
            pass

    await interaction.followup.send(f"Added {user.mention} to **{team_key}**.", ephemeral=True)


async def team_name_autocomplete(interaction: discord.Interaction, current: str):
    db = load_db()
    return [
        app_commands.Choice(name=key, value=key)
        for key in db["teams"].keys()
        if current.lower() in key.lower()
    ][:25]


@bot.tree.command(
    name="staffchangesetting",
    description="(Staff) Change a team's name, colour, or icon, kick a member, or delete it",
)
@app_commands.describe(
    team="Team to modify",
    delete="Delete the team — removes the role, channel, and database entry (can't be undone)",
    changename="New team name",
    changecolour="New hex colour for the team's role, e.g. #5865F2",
    changeicon="New single standard emoji for the team (no custom server emojis)",
    kick="A member of the team to remove",
)
async def staffchangesetting(
    interaction: discord.Interaction,
    team: str,
    delete: bool = False,
    changename: str = None,
    changecolour: str = None,
    changeicon: str = None,
    kick: discord.Member = None,
):
    await interaction.response.defer(ephemeral=True)

    if not has_staff_role(interaction.user):
        await interaction.followup.send("You don't have permission to use this command.", ephemeral=True)
        return

    db = load_db()
    team_key = find_team_key_ci(db["teams"], team)
    if not team_key:
        await interaction.followup.send("No team found with that name.", ephemeral=True)
        return

    if delete:
        view = ConfirmDeleteTeamView(interaction.user.id, team_key, interaction.guild)
        await interaction.followup.send(
            f"Are you sure you want to delete **{team_key}**? This will remove the team's role, "
            f"channel, and database entry, and can't be undone.",
            view=view,
            ephemeral=True,
        )
        return

    if not any([changename, changecolour, changeicon, kick]):
        await interaction.followup.send(
            "You didn't specify anything to change. Provide `changename`, `changecolour`, "
            "`changeicon`, `kick`, or set `delete:` to True.",
            ephemeral=True,
        )
        return

    info = db["teams"][team_key]

    if kick is not None and kick.id not in info.get("members", []):
        await interaction.followup.send(f"{kick.mention} isn't a member of **{team_key}**.", ephemeral=True)
        return

    if changename and changename.lower() != team_key.lower() and find_team_key_ci(db["teams"], changename):
        await interaction.followup.send(
            f"A team called **{changename}** already exists. Pick a different name.", ephemeral=True
        )
        return

    normalized_colour = None
    if changecolour:
        normalized_colour = normalize_hex_colour(changecolour)
        if normalized_colour is None:
            await interaction.followup.send(
                "That's not a valid hex colour. Use a format like `#5865F2`.", ephemeral=True
            )
            return

    if changeicon and not is_valid_standard_emoji(changeicon):
        await interaction.followup.send(
            "That's not a standard Discord emoji. Please use a single regular emoji "
            "(custom server emojis can't be used in channel names or role icons).",
            ephemeral=True,
        )
        return

    info = db["teams"][team_key]
    role = interaction.guild.get_role(info["role_id"])
    channel = interaction.guild.get_channel(info["channel_id"])

    new_name = changename if changename else team_key
    new_emoji = changeicon if changeicon else info["emoji"]

    role_edit_kwargs = {}
    if changename:
        role_edit_kwargs["name"] = f"{new_name} Team"
    if changecolour:
        role_edit_kwargs["colour"] = discord.Colour.from_str(normalized_colour)
    if changeicon:
        role_edit_kwargs["display_icon"] = new_emoji

    icon_warning = None
    if role and role_edit_kwargs:
        try:
            await role.edit(reason=f"Team settings changed by staff member {interaction.user}", **role_edit_kwargs)
        except discord.HTTPException:
            if "display_icon" in role_edit_kwargs:
                # Role icons require a certain server boost level; retry without it
                role_edit_kwargs.pop("display_icon")
                icon_warning = "couldn't set the role icon (requires a certain server boost level)"
                if role_edit_kwargs:
                    try:
                        await role.edit(
                            reason=f"Team settings changed by staff member {interaction.user}",
                            **role_edit_kwargs,
                        )
                    except discord.HTTPException:
                        await interaction.followup.send(
                            "Couldn't apply those changes — Discord rejected the request.", ephemeral=True
                        )
                        return
            else:
                await interaction.followup.send(
                    "Couldn't apply those changes — Discord rejected the request.", ephemeral=True
                )
                return

    if channel and (changename or changeicon):
        try:
            await channel.edit(
                name=f"{new_emoji}┃{new_name}-Team",
                reason=f"Team settings changed by staff member {interaction.user}",
            )
        except discord.HTTPException:
            await interaction.followup.send(
                "Updated the role, but couldn't rename the channel — Discord rejected the new "
                "name (check length/characters). Team may now be inconsistently named.",
                ephemeral=True,
            )
            return

    if kick is not None:
        await perform_team_kick(
            db, team_key, kick.id, interaction.guild, reason=f"Kicked from team by staff member {interaction.user}"
        )

    renamed = changename and new_name.lower() != team_key.lower()
    if renamed:
        db["teams"][new_name] = info
        del db["teams"][team_key]
        team_key = new_name
    if changeicon:
        db["teams"][team_key]["emoji"] = new_emoji

    save_db(db)
    await backup_db_to_log_channel()
    if renamed or changeicon:
        try:
            await repost_tournament_panel()
        except Exception as e:
            print(f"[ERROR] Failed to refresh tournament panel after team settings change: {e}")

    changes = []
    if changename:
        changes.append(f"name → **{new_name}**")
    if changecolour:
        changes.append(f"colour → `{normalized_colour}`")
    if changeicon:
        changes.append(f"icon → {new_emoji}")
    if kick is not None:
        changes.append(f"kicked {kick.mention}")

    if changename or changecolour or changeicon:
        await log_team_event(
            "⚙️ Team Settings Changed (Staff)",
            colour=discord.Colour.blue(),
            fields=[
                ("Team", team_key, True),
                ("Changed By", f"{interaction.user.mention} (`{interaction.user.id}`)", True),
                ("Changes", ", ".join(c for c in changes if not c.startswith("kicked")), False),
            ],
        )

    message = f"✅ Updated **{team_key}**: " + ", ".join(changes)
    if icon_warning:
        message += f"\n⚠️ Everything else applied, but {icon_warning}."
    await interaction.followup.send(message, ephemeral=True)


staffchangesetting.autocomplete("team")(team_name_autocomplete)
forceadd.autocomplete("team")(team_name_autocomplete)


@bot.tree.command(
    name="bypassteamlimit",
    description="(Staff) Let a team exceed the normal 10-member cap",
)
@app_commands.describe(
    team="Team to update",
    enabled="Whether the team can exceed the normal member cap (default: True)",
)
async def bypassteamlimit(interaction: discord.Interaction, team: str, enabled: bool = True):
    await interaction.response.defer(ephemeral=True)

    if not has_staff_role(interaction.user):
        await interaction.followup.send("You don't have permission to use this command.", ephemeral=True)
        return

    db = load_db()
    team_key = find_team_key_ci(db["teams"], team)
    if not team_key:
        await interaction.followup.send("No team found with that name.", ephemeral=True)
        return

    info = db["teams"][team_key]
    info["bypass_member_limit"] = enabled
    save_db(db)
    await backup_db_to_log_channel()

    await log_team_event(
        "🔧 Member Cap Bypass Toggled",
        colour=discord.Colour.blue(),
        fields=[
            ("Team", team_key, True),
            ("Bypass Enabled", str(enabled), True),
            ("Changed By", f"{interaction.user.mention} (`{interaction.user.id}`)", True),
        ],
    )

    if enabled:
        await interaction.followup.send(
            f"✅ **{team_key}** can now have more than {MAX_TEAM_MEMBERS} members.", ephemeral=True
        )
    else:
        await interaction.followup.send(
            f"✅ **{team_key}** is back to the normal {MAX_TEAM_MEMBERS}-member cap.", ephemeral=True
        )


bypassteamlimit.autocomplete("team")(team_name_autocomplete)


@bot.tree.command(
    name="cleanup",
    description="(Staff) Delete teams that only have their leader and no other members",
)
async def cleanup(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    if not has_staff_role(interaction.user):
        await interaction.followup.send("You don't have permission to use this command.", ephemeral=True)
        return

    db = load_db()
    solo_teams = [
        team_name
        for team_name, info in db["teams"].items()
        if len(info.get("members", [])) == 1 and info["members"][0] == info.get("leader_id")
    ]

    if not solo_teams:
        await interaction.followup.send(
            "No teams found with only the leader on them — nothing to clean up.", ephemeral=True
        )
        return

    preview_limit = 20
    lines = []
    for team_name in solo_teams[:preview_limit]:
        info = db["teams"][team_name]
        leader_id = info.get("leader_id")
        lines.append(f"• **{team_name}** — leader <@{leader_id}>")
    if len(solo_teams) > preview_limit:
        lines.append(f"…and {len(solo_teams) - preview_limit} more")

    view = ConfirmSoloTeamCleanupView(interaction.user.id, solo_teams, interaction.guild)
    await interaction.followup.send(
        f"Found **{len(solo_teams)}** team(s) with only the leader on them:\n" + "\n".join(lines) +
        "\n\nDelete them (role, channel, and database entry)? This can't be undone.",
        view=view,
        ephemeral=True,
    )


@bot.tree.command(
    name="cleanuporphanteams",
    description="(Staff) Delete channels/roles in the team category that have no matching database entry",
)
async def cleanuporphanteams(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    if not has_staff_role(interaction.user):
        await interaction.followup.send("You don't have permission to use this command.", ephemeral=True)
        return

    categories = []
    for cat_id in (TEAM_CATEGORY_ID, TEAM_CATEGORY_OVERFLOW_ID):
        cat = interaction.guild.get_channel(cat_id)
        if cat is not None and isinstance(cat, discord.CategoryChannel):
            categories.append(cat)

    if not categories:
        await interaction.followup.send("Couldn't find either team category.", ephemeral=True)
        return

    db = load_db()
    known_channel_ids = {info["channel_id"] for info in db["teams"].values()}

    orphans = []  # list of (channel, role_or_None)
    for category in categories:
        for channel in category.channels:
            if channel.id in known_channel_ids:
                continue
            linked_role = None
            for target, overwrite in channel.overwrites.items():
                if isinstance(target, discord.Role) and target.id != interaction.guild.default_role.id:
                    allow, _deny = overwrite.pair()
                    if allow.view_channel:
                        linked_role = target
                        break
            orphans.append((channel, linked_role))

    if not orphans:
        await interaction.followup.send(
            "No orphaned team channels found — everything in both team categories matches the database.",
            ephemeral=True,
        )
        return

    preview_limit = 20
    lines = []
    for channel, role in orphans[:preview_limit]:
        role_part = f" + role **{role.name}**" if role else " (no linked role found)"
        lines.append(f"• {channel.mention}{role_part}")
    if len(orphans) > preview_limit:
        lines.append(f"…and {len(orphans) - preview_limit} more")

    view = ConfirmCleanupView(interaction.user.id, orphans)
    await interaction.followup.send(
        f"Found **{len(orphans)}** channel(s) across both team categories with no matching "
        f"database entry:\n" + "\n".join(lines) + "\n\nDelete them (and their linked roles)? This can't be undone.",
        view=view,
        ephemeral=True,
    )


@bot.tree.command(
    name="syncteammembers",
    description="(Staff) Remove database members from a team if they no longer hold that team's role",
)
async def syncteammembers(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    if not has_staff_role(interaction.user):
        await interaction.followup.send("You don't have permission to use this command.", ephemeral=True)
        return

    db = load_db()
    if not db["teams"]:
        await interaction.followup.send("There are no teams to sync.", ephemeral=True)
        return

    report_lines = []
    total_removed = 0
    changed = False

    for team_name, info in db["teams"].items():
        role = interaction.guild.get_role(info.get("role_id"))
        if role is None:
            report_lines.append(f"⚠️ **{team_name}**: role no longer exists — skipped")
            continue

        # role.members reflects who actually currently holds the role right now (relies on
        # the member cache, which the members intent keeps populated).
        current_role_member_ids = {member.id for member in role.members}
        members_list = info.get("members", [])
        to_remove = [uid for uid in members_list if uid not in current_role_member_ids]

        if not to_remove:
            continue

        info["members"] = [uid for uid in members_list if uid in current_role_member_ids]
        for uid in to_remove:
            clear_team_join(info, uid)

        changed = True
        total_removed += len(to_remove)
        mentions = ", ".join(f"<@{uid}>" for uid in to_remove)
        leader_note = ""
        if info.get("leader_id") in to_remove:
            leader_note = " ⚠️ **includes the team leader** — you may want to check this team"
        report_lines.append(f"**{team_name}**: removed {len(to_remove)} — {mentions}{leader_note}")

    if not changed:
        await interaction.followup.send(
            "✅ Everything's already in sync — every database member still holds their team's role.",
            ephemeral=True,
        )
        return

    save_db(db)
    await backup_db_to_log_channel()

    preview_limit = 15
    lines = report_lines[:preview_limit]
    if len(report_lines) > preview_limit:
        lines.append(f"…and {len(report_lines) - preview_limit} more line(s)")

    await interaction.followup.send(
        f"🔄 Synced team membership — removed {total_removed} member(s) across the database "
        f"who no longer hold their team's role:\n" + "\n".join(lines),
        ephemeral=True,
    )


@bot.tree.command(
    name="randomgiverole",
    description="(Staff) Give a role to a random selection of members",
)
@app_commands.describe(
    role="The role to hand out",
    number="How many random members should receive it (capped automatically at the eligible member count)",
)
async def randomgiverole(
    interaction: discord.Interaction, role: discord.Role, number: app_commands.Range[int, 1, 1000000]
):
    await interaction.response.defer(ephemeral=True)

    if not has_staff_role(interaction.user):
        await interaction.followup.send("You don't have permission to use this command.", ephemeral=True)
        return

    if role.is_default() or role.managed:
        await interaction.followup.send(
            "Can't hand out `@everyone` or a bot/integration-managed role.", ephemeral=True
        )
        return

    if role >= interaction.guild.me.top_role:
        await interaction.followup.send(
            f"I can't assign {role.mention} — it's positioned above (or equal to) my own top role. "
            f"Move my role above it and try again.",
            ephemeral=True,
        )
        return

    # Only real members who don't already have the role are eligible to win it.
    eligible = [member for member in interaction.guild.members if not member.bot and role not in member.roles]

    if not eligible:
        await interaction.followup.send(
            f"No eligible members — everyone (or nobody) already has {role.mention}.", ephemeral=True
        )
        return

    import random

    pick_count = min(number, len(eligible))
    chosen = random.sample(eligible, pick_count)

    granted = []
    failed = []
    for member in chosen:
        try:
            await member.add_roles(role, reason=f"/randomgiverole by {interaction.user}")
            granted.append(member)
        except discord.HTTPException:
            failed.append(member)

    result = f"🎲 Gave {role.mention} to {len(granted)} random member(s):\n" + ", ".join(
        m.mention for m in granted
    )
    if pick_count < number:
        result += f"\n\n⚠️ Only {len(eligible)} member(s) were eligible, so fewer than {number} were picked."
    if failed:
        result += f"\n⚠️ Couldn't assign the role to: {', '.join(m.mention for m in failed)}."

    await interaction.followup.send(result, ephemeral=True)


# ---------- Invite tracking (built from the Invite Tracker app's messages) ----------
# We don't call Discord's native invite API directly — instead we read the plain-text
# join/leave messages the Invite Tracker app already posts in INVITE_TRACKER_CHANNEL_ID
# and turn those into a JSON "database" of who invited whom, backed up the same way
# teams/giveaways/tickets are (an auto-updated message in INVITE_LOG_CHANNEL_ID).
#
# Known message shapes we parse (Invite Tracker's exact wording, matched literally):
#   "{user} joined using a vanity invite."
#   "{user} left the server. They joined using the vanity invite."
#   "{user} left the server, they were invited by {inviter}."
#   "{user} has been invited by {inviter} and has now {count} invites."
_INVITE_JOIN_INVITED_RE = re.compile(
    r"^(?P<user>.+?) has been invited by (?P<inviter>.+?) and has now (?P<count>\d+) invites?\.?\s*$"
)
_INVITE_LEFT_INVITED_RE = re.compile(
    r"^(?P<user>.+?) left the server, they were invited by (?P<inviter>.+?)\.?\s*$"
)
_INVITE_LEFT_VANITY_RE = re.compile(
    r"^(?P<user>.+?) left the server\.\s*They joined using the vanity invite\.?\s*$"
)
_INVITE_JOIN_VANITY_RE = re.compile(r"^(?P<user>.+?) joined using a vanity invite\.?\s*$")
_MENTION_ID_RE = re.compile(r"<@!?(\d+)>")


def _extract_mention_id(text: str) -> int | None:
    m = _MENTION_ID_RE.search(text)
    return int(m.group(1)) if m else None


def _clean_invite_name(text: str) -> str:
    stripped = _MENTION_ID_RE.sub("", text).strip()
    return stripped or text.strip()


def parse_invite_tracker_line(line: str) -> dict | None:
    """Matches a single line of Invite Tracker text against the known message shapes.
    Returns a small event dict, or None if the line doesn't match anything we recognise
    (e.g. an unrelated message that happens to land in the same channel)."""
    line = line.strip()
    if not line:
        return None

    m = _INVITE_JOIN_INVITED_RE.match(line)
    if m:
        return {
            "kind": "join",
            "method": "invite",
            "user_raw": m.group("user"),
            "inviter_raw": m.group("inviter"),
            "invite_count": int(m.group("count")),
        }

    m = _INVITE_LEFT_INVITED_RE.match(line)
    if m:
        return {"kind": "left", "method": "invite", "user_raw": m.group("user"), "inviter_raw": m.group("inviter")}

    m = _INVITE_LEFT_VANITY_RE.match(line)
    if m:
        return {"kind": "left", "method": "vanity", "user_raw": m.group("user"), "inviter_raw": None}

    m = _INVITE_JOIN_VANITY_RE.match(line)
    if m:
        return {"kind": "join", "method": "vanity", "user_raw": m.group("user"), "inviter_raw": None}

    return None


def _resolve_member_id_by_name(guild: discord.Guild | None, name_text: str) -> int | None:
    """Best-effort lookup — Invite Tracker names the joining/leaving user as plain text
    (not a mention) in most message shapes, so this is the only way to attach a real user
    ID for those. Relies on the member cache (intents.members is enabled), and can miss or
    mismatch if someone's changed their name/nickname since the message was posted."""
    if guild is None or not name_text:
        return None
    name_clean = name_text.strip()
    member = discord.utils.find(
        lambda m: name_clean in (m.display_name, m.name, str(m)),
        guild.members,
    )
    return member.id if member else None


def _invite_record_key(user_id: int | None, name_text: str) -> str:
    if user_id is not None:
        return str(user_id)
    return f"name:{name_text.strip().lower()}"


def apply_invite_event(
    db: dict, event: dict, timestamp: str, message_id: int, guild: discord.Guild | None
) -> None:
    """Folds one parsed event into db["invited_users"], keyed by user ID when we can
    resolve one, falling back to a lowercased-name key otherwise."""
    invited_users = db.setdefault("invited_users", {})

    user_mention_id = _extract_mention_id(event["user_raw"])
    user_name = _clean_invite_name(event["user_raw"])
    user_id = user_mention_id or _resolve_member_id_by_name(guild, user_name)

    inviter_name = "vanity"
    inviter_id = None
    if event.get("inviter_raw"):
        inviter_name = _clean_invite_name(event["inviter_raw"])
        inviter_id = _extract_mention_id(event["inviter_raw"]) or _resolve_member_id_by_name(guild, inviter_name)

    key = _invite_record_key(user_id, user_name)
    record = invited_users.get(key, {})
    record["user_name"] = user_name
    if user_id is not None:
        record["user_id"] = user_id
    else:
        record.setdefault("user_id", None)

    if event["kind"] == "join":
        record["inviter_name"] = inviter_name
        record["inviter_id"] = inviter_id
        record["method"] = event["method"]
        record["joined_at"] = timestamp
        record["joined_message_id"] = message_id
        record["still_in_server"] = True
        if event["method"] == "invite" and "invite_count" in event:
            record["inviter_invite_count"] = event["invite_count"]
    else:  # left
        # A leave message is a weaker signal than a join message for who the inviter was —
        # only fill it in if we don't already have it from an earlier join event.
        record.setdefault("inviter_name", inviter_name)
        record.setdefault("inviter_id", inviter_id)
        record.setdefault("method", event["method"])
        record["left_at"] = timestamp
        record["left_message_id"] = message_id
        record["still_in_server"] = False

    invited_users[key] = record


def load_invite_db() -> dict:
    data = _load_json_file(INVITES_DB_FILE)
    if data is None:
        return {"invited_users": {}, "last_processed_message_id": None}
    data.setdefault("invited_users", {})
    data.setdefault("last_processed_message_id", None)
    return data


def save_invite_db(data: dict) -> None:
    data["last_updated"] = discord.utils.utcnow().isoformat()
    _atomic_write_json(INVITES_DB_FILE, data)


async def backup_invite_db_to_log_channel():
    try:
        await _backup_file_to_channel(INVITE_LOG_CHANNEL_ID, INVITES_DB_FILE, INVITES_DB_FILE)
    except discord.HTTPException as e:
        print(f"Failed to back up invite db to log channel: {e}")
    except Exception as e:  # noqa: BLE001 - never let a bad backup attempt kill anything else
        print(f"Unexpected error backing up invite db: {e}")


async def restore_invite_db_from_log_channel():
    if _load_json_file(INVITES_DB_FILE) is not None:
        # Local data already present (e.g. a crash-restart, not a fresh container) — push
        # it straight to the log channel so the backup there is confirmed up to date.
        await backup_invite_db_to_log_channel()
        return
    try:
        found = await _restore_file_from_channel(INVITE_LOG_CHANNEL_ID, INVITES_DB_FILE, INVITES_DB_FILE)
    except discord.HTTPException as e:
        print(f"Failed to restore invite db from log channel: {e}")
        found = False
    if found:
        print("Restored invite db from log channel backup.")
    else:
        print("No existing invite db backup found — starting fresh.")


# ============================================================
# SCRIMS — /startscrim challenges another team's leader by DM;
# if they accept, a temporary 🎯┃scrim channel is created that
# both teams can see and type in, and it auto-deletes itself
# SCRIM_DURATION_DAYS days after creation.
# ============================================================

def load_scrim_db() -> dict:
    data = _load_json_file(SCRIMS_DB_FILE)
    if data is None:
        return {"scrims": {}}
    data.setdefault("scrims", {})
    return data


def save_scrim_db(data: dict) -> None:
    data["last_updated"] = discord.utils.utcnow().isoformat()
    _atomic_write_json(SCRIMS_DB_FILE, data)


async def backup_scrim_db_to_log_channel():
    try:
        await _backup_file_to_channel(SCRIM_LOG_CHANNEL_ID, SCRIMS_DB_FILE, SCRIMS_DB_FILE)
    except discord.HTTPException as e:
        print(f"Failed to back up scrim db to log channel: {e}")
    except Exception as e:  # noqa: BLE001 - never let a bad backup attempt kill anything else
        print(f"Unexpected error backing up scrim db: {e}")


async def restore_scrim_db_from_log_channel():
    if _load_json_file(SCRIMS_DB_FILE) is not None:
        # Local data already present (e.g. a crash-restart, not a fresh container) — push
        # it straight to the log channel so the backup there is confirmed up to date.
        await backup_scrim_db_to_log_channel()
        return
    try:
        found = await _restore_file_from_channel(SCRIM_LOG_CHANNEL_ID, SCRIMS_DB_FILE, SCRIMS_DB_FILE)
    except discord.HTTPException as e:
        print(f"Failed to restore scrim db from log channel: {e}")
        found = False
    if found:
        print("Restored scrim db from log channel backup.")
    else:
        print("No existing scrim db backup found — starting fresh.")


def scrim_leader_channel_overwrite() -> discord.PermissionOverwrite:
    """Permissions granted to each team's leader in their scrim channel: on top of
    viewing/sending, mention_everyone lets them ping their own team's role even though
    the role itself isn't set to be mentionable."""
    return discord.PermissionOverwrite(
        view_channel=True,
        send_messages=True,
        read_message_history=True,
        mention_everyone=True,
    )


def scrim_member_channel_overwrite() -> discord.PermissionOverwrite:
    """Basic permissions granted to every other member of both teams in a scrim
    channel: they can see it, type in it, and read history, but not ping roles."""
    return discord.PermissionOverwrite(
        view_channel=True,
        send_messages=True,
        read_message_history=True,
        add_reactions=True,
    )


async def _process_invite_tracker_message(message: discord.Message) -> None:
    """Called live for every new message posted in INVITE_TRACKER_CHANNEL_ID — this is
    what keeps the database updating in real time as people join/leave, without needing
    a manual /syncinvites re-scan."""
    events = [e for e in (parse_invite_tracker_line(line) for line in message.content.split("\n")) if e]
    if not events:
        return

    db = load_invite_db()
    timestamp = message.created_at.isoformat()
    for event in events:
        apply_invite_event(db, event, timestamp, message.id, message.guild)
    db["last_processed_message_id"] = message.id
    save_invite_db(db)
    await backup_invite_db_to_log_channel()


class MemberCountView(discord.ui.LayoutView):
    """A Components V2 'container' — Discord's own rounded, accent-bordered card
    element — holding the live member count. Built fresh per send since the count
    changes; timeout=None because it's a one-off display, not something we need to
    keep listening on."""

    def __init__(self, member_count: int):
        super().__init__(timeout=None)
        label = "member" if member_count == 1 else "members"
        text = discord.ui.TextDisplay(f"**{member_count:,}** {label}")
        container = discord.ui.Container(text)
        self.add_item(container)


@bot.event
async def on_message(message: discord.Message):
    if (
        message.guild is not None
        and not message.author.bot
        and message.content.strip().lower() == ".membercount"
    ):
        try:
            await message.delete()
        except discord.HTTPException:
            pass
        try:
            await message.channel.send(view=MemberCountView(message.guild.member_count))
        except Exception as e:  # noqa: BLE001 - don't let a rendering hiccup go unlogged
            print(f"Failed to send member count container: {e}")
        return

    if message.channel.id == INVITE_TRACKER_CHANNEL_ID and message.author.bot:
        try:
            await _process_invite_tracker_message(message)
        except discord.HTTPException as e:
            print(f"Failed to process an invite tracker message: {e}")

    if message.guild is not None and not message.author.bot:
        await maybe_restick_tournament_message(message)

        # Shared 5s-per-user anti-spam gate for BOTH leaderboards: a message only counts if
        # this long has passed since that user's last counted message, anywhere in the guild.
        # Also gated on the privacy panel's opt-out: everyone is tracked by default, but a
        # user's ID in _data_optout_ids means they've opted out via "Toggle Activity Tracking".
        if message.author.id not in _data_optout_ids:
            now_ts = time.time()
            last_counted = _message_count_cooldowns.get(message.author.id)
            if last_counted is None or now_ts - last_counted >= MESSAGE_COUNT_SPAM_COOLDOWN_SECONDS:
                _message_count_cooldowns[message.author.id] = now_ts
                if message.channel.id == MESSAGE_LEADERBOARD_TRACKED_CHANNEL_ID:
                    record_message_for_leaderboard(message.author.id)
                record_overall_message(message.author.id)
                await record_streak_message(message.author.id)

    await bot.process_commands(message)


async def disable_team_premium(guild: discord.Guild, team_key: str, info: dict, reason: str) -> None:
    """Reverts a team's role to its default (non-premium) look — drops the gradient
    secondary colour and swaps the custom role icon back to the team's plain emoji — and
    posts the disabled notice in the team's channel. Caller is responsible for setting
    info['premium'] = False, saving the DB, and backing it up."""
    role = guild.get_role(info.get("role_id"))
    if role is not None:
        try:
            await role.edit(secondary_colour=None, display_icon=info.get("emoji"), reason=reason)
        except discord.HTTPException:
            try:
                await role.edit(secondary_colour=None, reason=reason)
            except discord.HTTPException as e:
                print(f"[ERROR] Failed to revert role styling for {team_key}: {e}")

        reference_role = guild.get_role(REFERENCE_ROLE_ID)
        if reference_role is not None:
            try:
                await role.edit(position=reference_role.position + 1, reason=reason)
            except discord.HTTPException:
                pass  # bot's own top role may be too low to move things this high; skip silently

    channel = guild.get_channel(info.get("channel_id"))
    if channel is not None:
        try:
            await channel.send("<:SeaBanditPug:1528566122440294471> **Premium has been disabled.**")
        except discord.HTTPException:
            pass


async def audit_premium_teams() -> None:
    """Startup pass: for every team with premium styling active, verifies the team leader
    still holds one of the premium-granting roles (PREMIUM_ROLE_ID / PREMIUM_ROLE_ID_2). If
    they don't — e.g. they unboosted, lost the role, or left the server while the bot was
    offline — downgrades the team back to default, same as the live on_member_update check
    below. This just catches anything missed while the bot wasn't running."""
    db = load_db()
    if not db["teams"]:
        return

    guild = None
    downgraded = 0
    for team_key, info in db["teams"].items():
        if not info.get("premium"):
            continue

        channel_id = info.get("channel_id")
        if guild is None and channel_id is not None:
            try:
                # all teams live in one guild for this bot; grab it from any known channel
                seed_channel = bot.get_channel(channel_id) or await bot.fetch_channel(channel_id)
                guild = seed_channel.guild
            except discord.HTTPException:
                continue
        if guild is None:
            continue

        leader_id = info.get("leader_id")
        leader = guild.get_member(leader_id) if leader_id is not None else None
        if leader is None and leader_id is not None:
            try:
                leader = await guild.fetch_member(leader_id)
            except discord.HTTPException:
                leader = None  # left the server, or otherwise unresolvable — treat as no access

        has_premium_role = leader is not None and any(
            r.id in (PREMIUM_ROLE_ID, PREMIUM_ROLE_ID_2) for r in leader.roles
        )
        if has_premium_role:
            continue

        info["premium"] = False
        try:
            await disable_team_premium(
                guild, team_key, info,
                reason="Team leader no longer holds a premium role (checked on startup)",
            )
        except Exception as e:
            print(f"[ERROR] Failed to downgrade premium for {team_key} on startup: {e}")
        downgraded += 1

    if downgraded:
        save_db(db)
        await backup_db_to_log_channel()
        print(f"Downgraded premium styling for {downgraded} team(s) whose leader no longer has a premium role.")


@bot.event
async def on_member_update(before: discord.Member, after: discord.Member):
    had_premium_role = any(r.id in (PREMIUM_ROLE_ID, PREMIUM_ROLE_ID_2) for r in before.roles)
    has_premium_role = any(r.id in (PREMIUM_ROLE_ID, PREMIUM_ROLE_ID_2) for r in after.roles)
    if not (had_premium_role and not has_premium_role):
        return  # nothing relevant changed (covers losing a boost, since that also drops the role)

    db = load_db()
    team_key = find_team_by_leader(db["teams"], after.id)
    if not team_key:
        return

    info = db["teams"][team_key]
    if not info.get("premium"):
        return  # team never had premium styling applied — nothing to revert

    info["premium"] = False
    save_db(db)
    await backup_db_to_log_channel()

    try:
        await disable_team_premium(
            after.guild, team_key, info, reason=f"{after} lost their premium role — team downgraded"
        )
    except Exception as e:
        print(f"[ERROR] Failed to downgrade premium for {team_key}: {e}")


@bot.tree.command(
    name="syncinvites",
    description="(Staff) Rebuild the invite database by rescanning the Invite Tracker channel's history",
)
async def syncinvites(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    if not has_staff_role(interaction.user):
        await interaction.followup.send("You don't have permission to use this command.", ephemeral=True)
        return

    channel = interaction.guild.get_channel(INVITE_TRACKER_CHANNEL_ID) or bot.get_channel(
        INVITE_TRACKER_CHANNEL_ID
    )
    if channel is None:
        try:
            channel = await bot.fetch_channel(INVITE_TRACKER_CHANNEL_ID)
        except discord.HTTPException:
            await interaction.followup.send(
                "Couldn't find the invite tracker channel — check INVITE_TRACKER_CHANNEL_ID.", ephemeral=True
            )
            return

    db = {"invited_users": {}, "last_processed_message_id": None}
    processed_messages = 0
    matched_events = 0
    last_message_id = None

    try:
        async for message in channel.history(limit=None, oldest_first=True):
            if not message.author.bot:
                continue
            timestamp = message.created_at.isoformat()
            found_any = False
            for line in message.content.split("\n"):
                parsed = parse_invite_tracker_line(line)
                if parsed:
                    apply_invite_event(db, parsed, timestamp, message.id, interaction.guild)
                    matched_events += 1
                    found_any = True
            if found_any:
                processed_messages += 1
            last_message_id = message.id
    except discord.Forbidden:
        await interaction.followup.send(
            "I don't have permission to read that channel's history.", ephemeral=True
        )
        return
    except discord.HTTPException as e:
        await interaction.followup.send(f"Couldn't read the invite tracker channel: {e}", ephemeral=True)
        return

    db["last_processed_message_id"] = last_message_id
    save_invite_db(db)
    await backup_invite_db_to_log_channel()

    await interaction.followup.send(
        f"✅ Rebuilt the invite database from {channel.mention} — matched {matched_events} join/leave "
        f"event(s) across {processed_messages} message(s), now tracking {len(db['invited_users'])} "
        f"invited member(s). Backed up to <#{INVITE_LOG_CHANNEL_ID}>.",
        ephemeral=True,
    )



@bot.tree.command(
    name="premiumteamsettings",
    description="(Premium) Apply gradient role colours or a custom role icon to your team",
)
@app_commands.describe(
    colour1="Primary role colour",
    colour1hex="Custom primary hex colour, e.g. #5865F2 — overrides colour1 if both are given",
    colour2="Secondary role colour — combined with colour1 this creates a gradient",
    colour2hex="Custom secondary hex colour, e.g. #5865F2 — overrides colour2 if both are given",
    roleicon="Image to use as the team role's icon",
)
@app_commands.choices(colour1=PREMIUM_COLOUR_CHOICES, colour2=PREMIUM_COLOUR_CHOICES)
async def premiumteamsettings(
    interaction: discord.Interaction,
    colour1: app_commands.Choice[str] = None,
    colour1hex: str = None,
    colour2: app_commands.Choice[str] = None,
    colour2hex: str = None,
    roleicon: discord.Attachment = None,
):
    await interaction.response.defer(ephemeral=True)

    if not has_premium_access(interaction.user):
        await interaction.followup.send("You don't have permission to use this command.", ephemeral=True)
        return

    db = load_db()
    team_key = find_team_by_leader_or_coleader(db["teams"], interaction.user.id)
    if not team_key:
        await interaction.followup.send("You must be a team leader or co-leader to use this command.", ephemeral=True)
        return

    if not any([colour1, colour1hex, colour2, colour2hex, roleicon]):
        await interaction.followup.send(
            "You didn't specify anything to change. Provide a colour (dropdown or hex) and/or "
            "`roleicon`.",
            ephemeral=True,
        )
        return

    if roleicon is not None and not (roleicon.content_type or "").startswith("image/"):
        await interaction.followup.send("`roleicon` needs to be an image file.", ephemeral=True)
        return

    resolved_colour1 = None
    colour1_label = None
    if colour1hex:
        resolved_colour1 = normalize_hex_colour(colour1hex)
        if resolved_colour1 is None:
            await interaction.followup.send(
                "`colour1hex` isn't a valid hex colour. Use a format like `#5865F2`.", ephemeral=True
            )
            return
        colour1_label = resolved_colour1
    elif colour1:
        resolved_colour1 = colour1.value
        colour1_label = colour1.name

    resolved_colour2 = None
    colour2_label = None
    if colour2hex:
        resolved_colour2 = normalize_hex_colour(colour2hex)
        if resolved_colour2 is None:
            await interaction.followup.send(
                "`colour2hex` isn't a valid hex colour. Use a format like `#5865F2`.", ephemeral=True
            )
            return
        colour2_label = resolved_colour2
    elif colour2:
        resolved_colour2 = colour2.value
        colour2_label = colour2.name

    info = db["teams"][team_key]
    role = interaction.guild.get_role(info["role_id"])
    channel = interaction.guild.get_channel(info["channel_id"])
    if role is None:
        await interaction.followup.send("That team's role no longer exists.", ephemeral=True)
        return

    role_edit_kwargs = {}
    if resolved_colour1:
        role_edit_kwargs["colour"] = discord.Colour.from_str(resolved_colour1)
    if resolved_colour2:
        role_edit_kwargs["secondary_colour"] = discord.Colour.from_str(resolved_colour2)

    icon_warning = None
    if roleicon is not None:
        temp_emoji = None
        try:
            image_bytes = await roleicon.read()
            safe_name = re.sub(r"[^A-Za-z0-9_]", "", team_key)[:20] or "team"
            temp_emoji = await interaction.guild.create_custom_emoji(
                name=f"tmp_{safe_name}"[:32],
                image=image_bytes,
                reason="Temporary emoji used to process a premium role icon",
            )
            processed_bytes = await temp_emoji.read()
            role_edit_kwargs["display_icon"] = processed_bytes
        except discord.HTTPException:
            icon_warning = "couldn't process the role icon image"
        finally:
            if temp_emoji is not None:
                try:
                    await temp_emoji.delete(reason="Cleanup after setting premium role icon")
                except discord.HTTPException:
                    pass

    gradient_warning = None
    if role_edit_kwargs:
        try:
            await role.edit(reason=f"Premium settings changed by {interaction.user}", **role_edit_kwargs)
        except discord.HTTPException:
            # Gradients and role icons need a certain server boost level; fall back to just the
            # primary colour rather than losing the whole update.
            fallback_kwargs = {}
            if "colour" in role_edit_kwargs:
                fallback_kwargs["colour"] = role_edit_kwargs["colour"]
            if fallback_kwargs:
                try:
                    await role.edit(
                        reason=f"Premium settings changed by {interaction.user}", **fallback_kwargs
                    )
                    gradient_warning = (
                        "some of those changes need a higher server boost level and weren't applied"
                    )
                except discord.HTTPException:
                    await interaction.followup.send(
                        "Couldn't apply those changes — Discord rejected the request.", ephemeral=True
                    )
                    return
            else:
                await interaction.followup.send(
                    "Couldn't apply those changes — Discord rejected the request.", ephemeral=True
                )
                return

    first_activation = not info.get("premium", False)
    if first_activation:
        info["premium"] = True
        if channel:
            try:
                await channel.send(
                    "<:Camera:1528219214345666621> **Premium Activated!** "
                    "<:CompanyCoins:1528218837030535394>"
                )
            except discord.HTTPException:
                pass

    premium_marker_role = interaction.guild.get_role(PREMIUM_ROLE_ID)
    if premium_marker_role is not None:
        try:
            await role.edit(
                position=premium_marker_role.position + 1,
                reason="Keep premium team role above the premium marker role",
            )
        except discord.HTTPException:
            pass

    save_db(db)
    await backup_db_to_log_channel()

    changes = []
    if colour1_label:
        changes.append(f"colour1 → {colour1_label}")
    if colour2_label:
        changes.append(f"colour2 → {colour2_label}")
    if roleicon is not None and "display_icon" in role_edit_kwargs:
        changes.append("icon updated")

    message = (
        f"✨ Updated **{team_key}**'s premium styling: " + ", ".join(changes)
        if changes
        else f"✨ Premium settings applied for **{team_key}**."
    )
    if icon_warning:
        message += f"\n⚠️ {icon_warning.capitalize()}."
    if gradient_warning:
        message += f"\n⚠️ {gradient_warning.capitalize()}."
    await interaction.followup.send(message, ephemeral=True)


@bot.tree.command(
    name="changeteamsettings",
    description="Change your team's name, colour, or icon, kick a member, or delete it (leader/co-leader)",
)
@app_commands.describe(
    delete="Delete your team — removes the role, channel, and database entry (leader only, can't be undone)",
    changename="New team name",
    changecolour="New hex colour for the team's role, e.g. #5865F2",
    changeicon="New single standard emoji for the team (no custom server emojis)",
    kick="A member of your team to remove (co-leaders can't kick the leader)",
)
async def changeteamsettings(
    interaction: discord.Interaction,
    delete: bool = False,
    changename: str = None,
    changecolour: str = None,
    changeicon: str = None,
    kick: discord.Member = None,
):
    await interaction.response.defer(ephemeral=True)

    db = load_db()
    team_key = find_team_by_leader_or_coleader(db["teams"], interaction.user.id)
    if not team_key:
        await interaction.followup.send("You must be a team leader or co-leader to use this command.", ephemeral=True)
        return

    info = db["teams"][team_key]
    is_leader = info.get("leader_id") == interaction.user.id

    if delete:
        if not is_leader:
            await interaction.followup.send("Only the team leader can delete the team.", ephemeral=True)
            return
        view = ConfirmDeleteTeamView(interaction.user.id, team_key, interaction.guild)
        await interaction.followup.send(
            f"Are you sure you want to delete **{team_key}**? This will remove the team's role, "
            f"channel, and database entry, and can't be undone.",
            view=view,
            ephemeral=True,
        )
        return

    if not any([changename, changecolour, changeicon, kick]):
        await interaction.followup.send(
            "You didn't specify anything to change. Provide `changename`, `changecolour`, "
            "`changeicon`, `kick`, or set `delete:` to True.",
            ephemeral=True,
        )
        return

    if kick is not None and kick.id == info.get("leader_id") and not is_leader:
        await interaction.followup.send("Co-leaders can't kick the team leader.", ephemeral=True)
        return

    if kick is not None:
        if kick.id == interaction.user.id:
            await interaction.followup.send(
                "You can't kick yourself. Use `delete:True` if you want that.", ephemeral=True
            )
            return
        if kick.id not in info.get("members", []):
            await interaction.followup.send(f"{kick.mention} isn't a member of **{team_key}**.", ephemeral=True)
            return

    if changename and changename.lower() != team_key.lower() and find_team_key_ci(db["teams"], changename):
        await interaction.followup.send(
            f"A team called **{changename}** already exists. Pick a different name.", ephemeral=True
        )
        return

    normalized_colour = None
    if changecolour:
        normalized_colour = normalize_hex_colour(changecolour)
        if normalized_colour is None:
            await interaction.followup.send(
                "That's not a valid hex colour. Use a format like `#5865F2`.", ephemeral=True
            )
            return

    if changeicon and not is_valid_standard_emoji(changeicon):
        await interaction.followup.send(
            "That's not a standard Discord emoji. Please use a single regular emoji "
            "(custom server emojis can't be used in channel names or role icons).",
            ephemeral=True,
        )
        return

    role = interaction.guild.get_role(info["role_id"])
    channel = interaction.guild.get_channel(info["channel_id"])

    new_name = changename if changename else team_key
    new_emoji = changeicon if changeicon else info["emoji"]

    role_edit_kwargs = {}
    if changename:
        role_edit_kwargs["name"] = f"{new_name} Team"
    if changecolour:
        role_edit_kwargs["colour"] = discord.Colour.from_str(normalized_colour)
    if changeicon:
        role_edit_kwargs["display_icon"] = new_emoji

    icon_warning = None
    if role and role_edit_kwargs:
        try:
            await role.edit(reason=f"Team settings changed by {interaction.user}", **role_edit_kwargs)
        except discord.HTTPException:
            if "display_icon" in role_edit_kwargs:
                # Role icons require a certain server boost level; retry without it
                role_edit_kwargs.pop("display_icon")
                icon_warning = "couldn't set the role icon (requires a certain server boost level)"
                if role_edit_kwargs:
                    try:
                        await role.edit(
                            reason=f"Team settings changed by {interaction.user}",
                            **role_edit_kwargs,
                        )
                    except discord.HTTPException:
                        await interaction.followup.send(
                            "Couldn't apply those changes — Discord rejected the request.", ephemeral=True
                        )
                        return
            else:
                await interaction.followup.send(
                    "Couldn't apply those changes — Discord rejected the request.", ephemeral=True
                )
                return

    if channel and (changename or changeicon):
        try:
            await channel.edit(
                name=f"{new_emoji}┃{new_name}-Team",
                reason=f"Team settings changed by {interaction.user}",
            )
        except discord.HTTPException:
            await interaction.followup.send(
                "Updated the role, but couldn't rename the channel — Discord rejected the new "
                "name (check length/characters). Team may now be inconsistently named.",
                ephemeral=True,
            )
            return

    if kick is not None:
        await perform_team_kick(
            db, team_key, kick.id, interaction.guild, reason=f"Kicked from team by {interaction.user}"
        )

    renamed = changename and new_name.lower() != team_key.lower()
    if renamed:
        db["teams"][new_name] = info
        del db["teams"][team_key]
        team_key = new_name
    if changeicon:
        db["teams"][team_key]["emoji"] = new_emoji

    save_db(db)
    await backup_db_to_log_channel()
    if renamed or changeicon:
        try:
            await repost_tournament_panel()
        except Exception as e:
            print(f"[ERROR] Failed to refresh tournament panel after team settings change: {e}")

    changes = []
    if changename:
        changes.append(f"name → **{new_name}**")
    if changecolour:
        changes.append(f"colour → `{normalized_colour}`")
    if changeicon:
        changes.append(f"icon → {new_emoji}")
    if kick is not None:
        changes.append(f"kicked {kick.mention}")

    if changename or changecolour or changeicon:
        await log_team_event(
            "⚙️ Team Settings Changed",
            colour=discord.Colour.blue(),
            fields=[
                ("Team", team_key, True),
                ("Changed By", f"{interaction.user.mention} (`{interaction.user.id}`)", True),
                ("Changes", ", ".join(c for c in changes if not c.startswith("kicked")), False),
            ],
        )

    message = f"✅ Updated **{team_key}**: " + ", ".join(changes)
    if icon_warning:
        message += f"\n⚠️ Everything else applied, but {icon_warning}."
    await interaction.followup.send(message, ephemeral=True)


@bot.tree.command(
    name="setcoleader",
    description="Set or clear your team's co-leader (leader only)",
)
@app_commands.describe(
    user="The member to make co-leader (must already be on your team) — omit to remove the current co-leader"
)
async def setcoleader(interaction: discord.Interaction, user: discord.Member = None):
    await interaction.response.defer(ephemeral=True)

    db = load_db()
    team_key = find_team_by_leader(db["teams"], interaction.user.id)
    if not team_key:
        await interaction.followup.send("You must be the team leader to use this command.", ephemeral=True)
        return

    info = db["teams"][team_key]

    if user is None:
        current_co_leader_id = info.get("co_leader_id")
        if current_co_leader_id is None:
            await interaction.followup.send("Your team doesn't currently have a co-leader.", ephemeral=True)
            return
        await _clear_team_coleader(db, team_key, interaction.guild, reason=f"Co-leader removed by {interaction.user}")
        await log_team_event(
            "🥈 Co-Leader Removed",
            colour=discord.Colour.orange(),
            fields=[
                ("Team", team_key, True),
                ("Former Co-Leader", f"<@{current_co_leader_id}> (`{current_co_leader_id}`)", True),
                ("Removed By", f"{interaction.user.mention} (`{interaction.user.id}`)", True),
            ],
        )
        await interaction.followup.send(f"<@{current_co_leader_id}> is no longer co-leader of **{team_key}**.", ephemeral=True)
        return

    if user.id == interaction.user.id:
        await interaction.followup.send("You're already the leader.", ephemeral=True)
        return
    if user.bot:
        await interaction.followup.send("You can't make a bot a co-leader.", ephemeral=True)
        return
    if user.id not in info.get("members", []):
        await interaction.followup.send(f"{user.mention} isn't a member of **{team_key}**.", ephemeral=True)
        return
    if info.get("co_leader_id") == user.id:
        await interaction.followup.send(f"{user.mention} is already the co-leader.", ephemeral=True)
        return

    # Only one co-leader slot per team — clear whoever currently holds it first.
    if info.get("co_leader_id") is not None:
        await _clear_team_coleader(db, team_key, interaction.guild, reason=f"Co-leader replaced by {interaction.user}")

    info["co_leader_id"] = user.id
    save_db(db)
    await backup_db_to_log_channel()

    channel = interaction.guild.get_channel(info.get("channel_id"))
    if channel is not None:
        try:
            await channel.set_permissions(
                user, overwrite=team_leader_channel_overwrite(), reason=f"Set as co-leader by {interaction.user}"
            )
        except discord.HTTPException:
            pass
        try:
            await channel.send(f"🥈 {user.mention} is now co-leader of the team!")
        except discord.HTTPException:
            pass

    co_leader_role = interaction.guild.get_role(TEAM_CO_LEADER_ROLE_ID)
    if co_leader_role is not None:
        try:
            await user.add_roles(co_leader_role, reason=f"Set as co-leader by {interaction.user}")
        except discord.HTTPException:
            pass

    await log_team_event(
        "🥈 Co-Leader Set",
        colour=discord.Colour.blue(),
        fields=[
            ("Team", team_key, True),
            ("New Co-Leader", f"{user.mention} (`{user.id}`)", True),
            ("Set By", f"{interaction.user.mention} (`{interaction.user.id}`)", True),
        ],
    )

    await interaction.followup.send(f"{user.mention} is now co-leader of **{team_key}**.", ephemeral=True)


# ---------- Leader promotion commands ----------
@bot.tree.command(
    name="leaderpromote",
    description="Promote a member of your team to leader (leader only)",
)
@app_commands.describe(member="The team member to promote to leader")
async def leaderpromote(interaction: discord.Interaction, member: discord.Member):
    await interaction.response.defer(ephemeral=True)

    db = load_db()
    team_key = find_team_by_leader(db["teams"], interaction.user.id)
    if not team_key:
        await interaction.followup.send("You must be a team leader to use this command.", ephemeral=True)
        return

    info = db["teams"][team_key]

    if member.id == interaction.user.id:
        await interaction.followup.send("You're already the leader.", ephemeral=True)
        return

    if member.bot:
        await interaction.followup.send("You can't promote a bot.", ephemeral=True)
        return

    if member.id not in info.get("members", []):
        await interaction.followup.send(f"{member.mention} isn't a member of **{team_key}**.", ephemeral=True)
        return

    view = ConfirmLeaderPromoteView(interaction.user.id, team_key, member.id, interaction.guild)
    await interaction.followup.send(
        f"Are you sure you want to make {member.mention} the new leader of **{team_key}**? "
        f"You'll be demoted to a regular member.",
        view=view,
        ephemeral=True,
    )


@bot.tree.command(
    name="staffleaderpromote",
    description="(Staff) Promote a member to leader of the specified team",
)
@app_commands.describe(team="Team to update", user="The member to promote to leader")
async def staffleaderpromote(interaction: discord.Interaction, team: str, user: discord.Member):
    await interaction.response.defer(ephemeral=True)

    if not has_staff_role(interaction.user):
        await interaction.followup.send("You don't have permission to use this command.", ephemeral=True)
        return

    db = load_db()
    team_key = find_team_key_ci(db["teams"], team)
    if not team_key:
        await interaction.followup.send("No team found with that name.", ephemeral=True)
        return

    info = db["teams"][team_key]

    if user.bot:
        await interaction.followup.send("You can't promote a bot.", ephemeral=True)
        return

    if info.get("leader_id") == user.id:
        await interaction.followup.send(f"{user.mention} is already the leader of **{team_key}**.", ephemeral=True)
        return

    if user.id not in info.get("members", []):
        await interaction.followup.send(f"{user.mention} isn't a member of **{team_key}**.", ephemeral=True)
        return

    await perform_leader_promotion(
        db, team_key, user.id, interaction.guild, reason=f"Leader promotion by staff member {interaction.user}"
    )

    await interaction.followup.send(f"✅ {user.mention} is now the leader of **{team_key}**.", ephemeral=True)

    channel = interaction.guild.get_channel(info.get("channel_id"))
    if channel is not None:
        try:
            await channel.send(f"👑 {user.mention} is now the leader of the team!")
        except discord.HTTPException:
            pass


staffleaderpromote.autocomplete("team")(team_name_autocomplete)


# ---------- Giveaway slash command ----------
@bot.tree.command(name="startgiveaway", description="(Staff) Start a giveaway in this channel")
@app_commands.describe(
    winners="How many winners will be picked",
    prize="What's being given away",
    ends="How long the giveaway runs, e.g. 10m, 2h, 1d, 1d12h",
    hosted="Who's hosting the giveaway (defaults to you)",
)
async def startgiveaway(
    interaction: discord.Interaction,
    winners: app_commands.Range[int, 1, 50],
    prize: str,
    ends: str,
    hosted: discord.Member = None,
):
    await interaction.response.defer(ephemeral=True)

    if not has_staff_role(interaction.user):
        await interaction.followup.send("You don't have permission to use this command.", ephemeral=True)
        return

    duration = parse_duration(ends)
    if duration is None:
        await interaction.followup.send(
            "Couldn't parse `ends` — use something like `10m`, `2h`, `1d`, or `1d12h`.", ephemeral=True
        )
        return

    host = hosted or interaction.user
    end_dt = discord.utils.utcnow() + duration
    end_ts = int(end_dt.timestamp())

    view = build_giveaway_view(
        prize=prize, winners_count=winners, host_id=host.id, end_ts=end_ts, entries_count=0,
    )

    if os.path.exists(SUPPORT_BANNER_PATH):
        file = discord.File(SUPPORT_BANNER_PATH, filename=SUPPORT_BANNER_FILENAME)
        sent = await interaction.channel.send(view=view, file=file)
    else:
        sent = await interaction.channel.send(view=view)

    db = load_db()
    db.setdefault("giveaways", {})
    db["giveaways"][str(sent.id)] = {
        "guild_id": interaction.guild.id,
        "channel_id": sent.channel.id,
        "prize": prize,
        "winners": winners,
        "host_id": host.id,
        "end_ts": end_ts,
        "entries": [],
        "ended": False,
    }
    save_db(db)
    await backup_db_to_log_channel()

    await interaction.followup.send(f"{GIVEAWAY_JOIN_EMOJI} Giveaway started in {sent.channel.mention}!", ephemeral=True)


@bot.tree.command(name="changegiveawayprize", description="(Staff) Change the prize on an existing giveaway")
@app_commands.describe(
    messagelink="Link to the giveaway message (right-click it → Copy Message Link)",
    prize="The new prize text",
)
async def changegiveawayprize(interaction: discord.Interaction, messagelink: str, prize: str):
    await interaction.response.defer(ephemeral=True)

    if not has_staff_role(interaction.user):
        await interaction.followup.send("You don't have permission to use this command.", ephemeral=True)
        return

    parsed = parse_message_link(messagelink)
    if parsed is None:
        await interaction.followup.send(
            "Couldn't read that as a message link — right-click the giveaway message and choose "
            "**Copy Message Link**, then paste it here.", ephemeral=True,
        )
        return
    channel_id, message_id = parsed

    db = load_db()
    giveaways = db.setdefault("giveaways", {})
    key = str(message_id)
    info = giveaways.get(key)
    if info is None:
        await interaction.followup.send("That message isn't a giveaway I have on record.", ephemeral=True)
        return

    channel = bot.get_channel(channel_id)
    if channel is None and interaction.guild is not None:
        channel = interaction.guild.get_channel(channel_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(channel_id)
        except discord.HTTPException:
            channel = None
    if channel is None:
        await interaction.followup.send("I couldn't access the channel that giveaway is in.", ephemeral=True)
        return

    try:
        message = await channel.fetch_message(message_id)
    except discord.HTTPException:
        message = None

    old_prize = info.get("prize")
    info["prize"] = prize
    save_db(db)
    await backup_db_to_log_channel()

    if message is not None:
        view = build_giveaway_view(
            prize=prize,
            winners_count=info.get("winners", 1),
            host_id=info.get("host_id"),
            end_ts=info.get("end_ts", 0),
            entries_count=len(info.get("entries", [])),
            winner_ids=info.get("winner_ids") if info.get("ended") else None,
        )
        try:
            await message.edit(view=view)
        except discord.HTTPException:
            await interaction.followup.send(
                f"Updated the prize in the database (`{old_prize}` → `{prize}`), but couldn't edit the "
                "giveaway message itself — you may need to update it manually.", ephemeral=True,
            )
            return

    await interaction.followup.send(f"{GIVEAWAY_JOIN_EMOJI} Prize updated to **{prize}**.", ephemeral=True)


# ---------- Message stats slash command ----------
@bot.tree.command(
    name="globalteammessage",
    description="(Staff) Send a message to every team's channel",
)
@app_commands.describe(
    message="The message to send to every team channel. Use (team) to ping that team's role, e.g. '(team) hi'",
    exclude1="Team to exclude from this broadcast (optional)",
    exclude2="Team to exclude from this broadcast (optional)",
    exclude3="Team to exclude from this broadcast (optional)",
    exclude4="Team to exclude from this broadcast (optional)",
    exclude5="Team to exclude from this broadcast (optional)",
)
async def globalteammessage(
    interaction: discord.Interaction,
    message: str,
    exclude1: str = None,
    exclude2: str = None,
    exclude3: str = None,
    exclude4: str = None,
    exclude5: str = None,
):
    await interaction.response.defer(ephemeral=True)

    if not has_staff_role(interaction.user):
        await interaction.followup.send("You don't have permission to use this command.", ephemeral=True)
        return

    db = load_db()
    if not db["teams"]:
        await interaction.followup.send("There are no teams to message.", ephemeral=True)
        return

    # Discord slash commands don't support a true multi-select for dynamic values, so
    # exclusions are up to 5 separate optional slots (each with autocomplete, same as the
    # `team` parameter elsewhere) instead of one true dropdown.
    requested_excludes = [name for name in (exclude1, exclude2, exclude3, exclude4, exclude5) if name]
    excluded_keys = set()
    unknown_excludes = []
    for name in requested_excludes:
        key = find_team_key_ci(db["teams"], name)
        if key is None:
            unknown_excludes.append(name)
        else:
            excluded_keys.add(key)

    sent = 0
    skipped = []
    failed_teams = []
    for team_name, info in db["teams"].items():
        if team_name in excluded_keys:
            skipped.append(team_name)
            continue
        channel = interaction.guild.get_channel(info.get("channel_id"))
        if channel is None:
            failed_teams.append(team_name)
            continue

        role = interaction.guild.get_role(info.get("role_id"))
        role_mention = role.mention if role is not None else f"**{team_name}**"
        text = message.replace("(team)", role_mention)

        try:
            await channel.send(content=text, allowed_mentions=discord.AllowedMentions(roles=True))
            sent += 1
        except discord.HTTPException:
            failed_teams.append(team_name)

    result = f"✅ Sent to {sent} team channel(s)."
    if skipped:
        result += f"\n🚫 Excluded: {', '.join(skipped)}."
    if unknown_excludes:
        result += f"\n⚠️ Couldn't match these to a team (ignored): {', '.join(unknown_excludes)}."
    if failed_teams:
        result += f"\n⚠️ Couldn't send to: {', '.join(failed_teams)}."
    await interaction.followup.send(result, ephemeral=True)


globalteammessage.autocomplete("exclude1")(team_name_autocomplete)
globalteammessage.autocomplete("exclude2")(team_name_autocomplete)
globalteammessage.autocomplete("exclude3")(team_name_autocomplete)
globalteammessage.autocomplete("exclude4")(team_name_autocomplete)
globalteammessage.autocomplete("exclude5")(team_name_autocomplete)


# ---------- Tournament selection notification ----------
async def multi_team_autocomplete(interaction: discord.Interaction, current: str):
    """Autocompletes a comma-separated list of team names one team at a time: whatever's
    already typed before the last comma is kept as-is, and only the piece being typed
    right now gets suggestions, so picking multiple teams still feels like a dropdown."""
    db = load_db()
    team_names = sorted(db["teams"].keys())

    if "," in current:
        prefix, _, last_part = current.rpartition(",")
        prefix = f"{prefix}, "
    else:
        prefix = ""
        last_part = current

    last_part_stripped = last_part.strip().lower()
    matches = [name for name in team_names if last_part_stripped in name.lower()]

    choices = []
    for name in matches[:25]:
        full_value = f"{prefix}{name}"
        choices.append(app_commands.Choice(name=full_value[:100], value=full_value[:100]))
    return choices


# Matches either a role mention like <@&123456789012345678> or a bare role ID typed on
# its own — lets /sendtournament accept a block of pasted role mentions, one per line.
_ROLE_MENTION_OR_ID_RE = re.compile(r"<@&(\d+)>|\b(\d{15,21})\b")


@bot.tree.command(
    name="sendtournament",
    description="(Staff) Tell one or more teams they've been selected for the upcoming tournament",
)
@app_commands.describe(
    teams="Team role mentions/IDs or names to notify — one per line or comma-separated"
)
async def sendtournament(interaction: discord.Interaction, teams: str):
    await interaction.response.defer(ephemeral=True)

    if not has_staff_role(interaction.user):
        await interaction.followup.send("You don't have permission to use this command.", ephemeral=True)
        return

    db = load_db()
    if not db["teams"]:
        await interaction.followup.send("There are no teams to notify.", ephemeral=True)
        return

    resolved_keys = []
    unknown = []
    seen = set()

    # First, pull out every role mention/ID (people commonly paste a block of <@&...>
    # role mentions, one per line).
    role_ids_found = [int(m.group(1) or m.group(2)) for m in _ROLE_MENTION_OR_ID_RE.finditer(teams)]
    for role_id in role_ids_found:
        key = find_team_by_role_id(db["teams"], role_id)
        if key is None:
            unknown.append(f"<@&{role_id}>")
        elif key not in seen:
            resolved_keys.append(key)
            seen.add(key)

    # Anything left over (with the mentions/IDs stripped out) is treated as plain team
    # names, comma- or newline-separated — still supports the old `Alpha, Bravo` style.
    remainder = _ROLE_MENTION_OR_ID_RE.sub(" ", teams)
    requested_names = [part.strip() for part in re.split(r"[,\n]+", remainder) if part.strip()]
    for name in requested_names:
        key = find_team_key_ci(db["teams"], name)
        if key is None:
            unknown.append(name)
        elif key not in seen:
            resolved_keys.append(key)
            seen.add(key)

    if not resolved_keys:
        detail = f": {', '.join(unknown)}" if unknown else ""
        await interaction.followup.send(f"Couldn't match any of those to a team{detail}", ephemeral=True)
        return

    notified = []
    failed_teams = []
    for team_name in resolved_keys:
        info = db["teams"][team_name]
        channel = interaction.guild.get_channel(info.get("channel_id"))
        role_id = info.get("role_id")
        if channel is None or role_id is None:
            failed_teams.append(team_name)
            continue

        message = (
            f"Hello <@&{role_id}> , you have been selected to play for the upcoming "
            f"tournament. So stay Tuned! {TOURNAMENT_SUB_EMOJI}"
        )
        try:
            await channel.send(content=message)
            notified.append(team_name)
        except discord.HTTPException:
            failed_teams.append(team_name)

    result = f"✅ Notified {len(notified)} team(s): {', '.join(notified)}." if notified else "No teams were notified."
    if unknown:
        result += f"\n⚠️ Couldn't match these to a team (ignored): {', '.join(unknown)}."
    if failed_teams:
        result += f"\n⚠️ Couldn't send to: {', '.join(failed_teams)}."
    await interaction.followup.send(result, ephemeral=True)


sendtournament.autocomplete("teams")(multi_team_autocomplete)


@bot.tree.command(
    name="deletetournamentsignups",
    description="(Staff) Delete the tournament sign-up message from every team channel that has one",
)
async def deletetournamentsignups(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    if not has_staff_role(interaction.user):
        await interaction.followup.send("You don't have permission to use this command.", ephemeral=True)
        return

    db = load_db()
    teams_with_signup = {
        team_key: info for team_key, info in db["teams"].items() if info.get("tournament_message_id")
    }
    if not teams_with_signup:
        await interaction.followup.send("No teams currently have a tournament sign-up message posted.", ephemeral=True)
        return

    deleted = []
    failed = []
    for team_key, info in teams_with_signup.items():
        channel = interaction.guild.get_channel(info.get("channel_id"))
        message_id = info.get("tournament_message_id")

        if channel is not None:
            try:
                msg = await channel.fetch_message(message_id)
                await msg.delete()
            except discord.NotFound:
                pass  # already gone — still clean up the DB reference below
            except discord.HTTPException:
                failed.append(team_key)
                continue

        info["tournament_message_id"] = None
        deleted.append(team_key)

    save_db(db)
    await backup_db_to_log_channel()

    result = (
        f"🗑️ Deleted the sign-up message from {len(deleted)} team channel(s): {', '.join(deleted)}."
        if deleted
        else "No sign-up messages were deleted."
    )
    if failed:
        result += f"\n⚠️ Couldn't delete for: {', '.join(failed)} — check the bot's permissions there."
    result += "\nThey won't be re-stuck until posted again from the tournament panel dropdown."
    await interaction.followup.send(result, ephemeral=True)


# ---------- Question of the Day ----------
@bot.tree.command(
    name="qotd",
    description="(Staff) Post a Question of the Day and open a discussion thread on it",
)
@app_commands.describe(question="The question to ask")
async def qotd(interaction: discord.Interaction, question: app_commands.Range[str, 1, 200]):
    await interaction.response.defer(ephemeral=True)

    if not has_staff_role(interaction.user):
        await interaction.followup.send("You don't have permission to use this command.", ephemeral=True)
        return

    channel = interaction.guild.get_channel(QOTD_CHANNEL_ID) or await bot.fetch_channel(QOTD_CHANNEL_ID)

    content = f"<@&{QOTD_PING_ROLE_ID}>\n\n**Question of the Day:**\n{question}"

    try:
        sent = await channel.send(content=content)
    except discord.HTTPException:
        await interaction.followup.send(
            "Couldn't post the question — check the bot's permissions in that channel.", ephemeral=True
        )
        return

    thread_name = f"QOTD: {question}"[:100]  # Discord caps thread names at 100 characters
    try:
        await sent.create_thread(name=thread_name, reason=f"QOTD opened by {interaction.user}")
    except discord.HTTPException:
        await interaction.followup.send(
            f"Posted in {channel.mention}, but couldn't open a thread on it — check the bot's "
            f"permissions there (needs Create Public Threads).",
            ephemeral=True,
        )
        return

    await interaction.followup.send(f"✅ Posted in {channel.mention} and opened a thread.", ephemeral=True)


# ---------- Global slash-command activity logging ----------
# Fires after every slash command finishes successfully. Logged separately from the
# per-feature activity logs (e.g. log_team_event) so BOT_LOG_CHANNEL_ID has a complete,
# chronological record of every command anyone runs, on top of the JSON sync and
# startup/restart logging.
@bot.event
async def on_app_command_completion(
    interaction: discord.Interaction,
    command: app_commands.Command | app_commands.ContextMenu,
):
    options_text = ""
    try:
        data = interaction.data or {}
        raw_options = data.get("options") or []
        # Slash commands nest subcommand/subcommand-group options one or two levels deep;
        # walk down to the actual argument list instead of assuming it's flat.
        while raw_options and raw_options[0].get("type") in (1, 2):  # SUB_COMMAND, SUB_COMMAND_GROUP
            raw_options = raw_options[0].get("options") or []
        parts = [f"{opt['name']}: {opt['value']}" for opt in raw_options if "value" in opt]
        options_text = ", ".join(parts)
    except Exception:
        options_text = ""

    where = interaction.channel.mention if interaction.channel else "DMs"
    description = f"Run by {interaction.user.mention} (`{interaction.user.id}`) in {where}"
    if options_text:
        description += f"\n**Options:** {options_text}"

    await log_bot_event(
        title=f"⚙️ /{command.qualified_name}",
        description=description,
        colour=discord.Colour.blurple(),
    )


# ---------- Global slash-command error handler ----------
# Without this, discord.py just logs a full traceback for every unhandled command error
# (interaction/message expiring mid-command, a bad Member argument, etc.) and the user is
# left staring at "This interaction failed" with no explanation. This catches the common,
# expected cases quietly and gives the user something useful, and still logs anything
# unexpected so real bugs aren't hidden.
@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    command_name = interaction.command.name if interaction.command else "unknown"

    original = getattr(error, "original", error)

    # The interaction (or a message it referenced) expired/vanished before we could
    # respond — e.g. Discord hiccups, or the bot was briefly slow. Nothing we can send
    # back at this point; just log it quietly instead of a full traceback.
    if isinstance(original, discord.NotFound):
        print(f"'{command_name}': interaction or message no longer exists ({original}).")
        return

    # A Member/User/Channel argument couldn't be resolved (e.g. autocomplete raced with
    # typed text, or the target left the server between typing and submitting).
    if isinstance(error, app_commands.TransformerError):
        message = "Couldn't resolve that option — please pick from the autocomplete/picker list and try again."
        try:
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
        except discord.HTTPException:
            pass
        return

    print(f"Unhandled error in '{command_name}': {error!r}")
    await log_bot_event(
        title=f"❌ /{command_name} failed",
        description=f"Run by {interaction.user.mention} (`{interaction.user.id}`)\n```{error!r}```"[:4000],
        colour=discord.Colour.red(),
    )
    try:
        message = "Something went wrong running that command. Please try again."
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
    except discord.HTTPException:
        pass


@bot.event
async def on_ready():
    _leaderboard_log_asset_status()
    await restore_db_from_log_channel()
    await restore_ticket_db_from_log_channel()
    await restore_meta_version_from_log_channel()
    await restore_invite_db_from_log_channel()
    await restore_scrim_db_from_log_channel()
    await restore_message_leaderboard_from_log_channel()
    await restore_overall_message_from_log_channel()
    await restore_optout_db_from_log_channel()
    await restore_streak_db_from_log_channel()
    bot.add_view(SupportPanelView())
    # Registers the "Close" button's custom_id against a callback so it keeps working on
    # existing ticket threads after a restart — called with no arguments, this just
    # builds a placeholder container; only the button's custom_id is used to route
    # the interaction on old messages, not this container's content.
    bot.add_view(TicketThreadView())
    bot.add_view(TournamentAdminPanelView())
    bot.add_view(TournamentSignupView())
    bot.add_view(GiveawayView())
    bot.add_view(PrivacyPanelView())
    await bot.tree.sync()
    try:
        await sync_existing_teams()
    except discord.HTTPException as e:
        print(f"Failed to sync existing teams (leader role/permissions): {e}")
    try:
        await audit_premium_teams()
    except Exception as e:
        print(f"Failed to audit premium teams: {e}")
    try:
        await refresh_support_ticket_panel()
    except discord.HTTPException as e:
        print(f"Failed to refresh support ticket panel: {e}")
    try:
        await refresh_privacy_panel()
    except discord.HTTPException as e:
        print(f"Failed to refresh privacy panel: {e}")
    try:
        await post_tournament_panel_if_missing()
    except discord.HTTPException as e:
        print(f"Failed to refresh tournament panel: {e}")
    try:
        await ensure_tournament_stickies()
    except Exception as e:
        print(f"Failed to backfill tournament sticky messages: {e}")
    if not check_giveaways.is_running():
        check_giveaways.start()
    if not meta_poll_loop.is_running():
        meta_poll_loop.start()
    if not check_scrim_expiry.is_running():
        check_scrim_expiry.start()
    if not message_leaderboard_save_loop.is_running():
        message_leaderboard_save_loop.start()
    if not message_leaderboard_rollover_loop.is_running():
        message_leaderboard_rollover_loop.start()
    if not overall_message_save_loop.is_running():
        overall_message_save_loop.start()
    if not streak_check_loop.is_running():
        streak_check_loop.start()
    print(f"Logged in as {bot.user} (id: {bot.user.id})")
    print("Slash commands synced.")

    global _startup_logged
    if not _startup_logged:
        _startup_logged = True
        await log_bot_event(
            title="🟢 Bot started",
            description=f"Logged in as {bot.user} and finished startup (db restore, panel refresh, background loops).",
            colour=discord.Colour.green(),
        )
    else:
        # Discord re-fired on_ready without the process actually restarting (e.g. a
        # gateway reconnect) — worth a quieter note rather than another "started" log.
        await log_bot_event(
            title="🔄 Gateway reconnected",
            description="on_ready fired again (session reconnect) — startup tasks re-ran.",
            colour=discord.Colour.gold(),
        )


if __name__ == "__main__":
    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        raise RuntimeError("DISCORD_TOKEN environment variable is not set")
    bot.run(token)
