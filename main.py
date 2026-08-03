from __future__ import annotations

import os
import io
import re
import json
import logging
from datetime import timedelta, datetime

import aiohttp
import emoji as emoji_lib
import discord
from discord import app_commands
from discord.ext import commands, tasks

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
TEAM_LOG_CHANNEL_ID = 1530008905663512626  # teams JSON "database" message lives here
GIVEAWAY_LOG_CHANNEL_ID = 1530009058294370476  # giveaways JSON "database" message lives here
LOG_CHANNEL_ID = 1528147225799037008       # legacy combined database channel — kept only so
                                            # old data can be migrated into the two channels above
REFERENCE_ROLE_ID = 1528009686509420616    # team roles are kept positioned just above this role
STAFF_ROLE_ID = 1528009567219224616        # only holders of this role can use staff team-management commands
PREMIUM_ROLE_ID = 1528139462159106059      # gates /premiumteamsettings; premium team roles are kept above this role
PREMIUM_ROLE_ID_2 = 1529805001088569384    # a second role that also grants premium access
CREATE_TEAM_ROLE_ID = 1528160422857932868  # required to use /createteam (pre-existing teams are grandfathered in)
TEAM_LEADER_ROLE_ID = 1528445357317423135  # granted to every team leader, current and future
MAX_TEAM_MEMBERS = 10                      # includes the leader
TEAM_JOIN_COOLDOWN_DAYS = 7                 # how long a member must stay on a team before leaving it
SUPPORT_TICKET_CHANNEL_ID = 1530456581903486996  # the support ticket panel is posted/refreshed here, and new ticket threads are opened here
TICKET_PING_ROLE_ID = 1528224254896771132        # pinged (alongside the opener) whenever a new ticket thread is opened
TICKET_LOG_CHANNEL_ID = 1533595017438826646       # ticket numbers/records JSON "database" message lives here
TICKET_CLOSE_ROLE_ID = 1528142703727083691        # holders of this role can close any ticket, same as staff
TOURNAMENT_PANEL_CHANNEL_ID = 1528515043992404150  # the tournament team-select panel is posted/refreshed here
TOURNAMENT_SUBMISSION_ROLE_ID = 1533580965094359211  # granted to everyone listed on a submitted tournament sheet
TOURNAMENT_CLEAR_PURGE_CHANNEL_ID = 1533581676184076398  # fully purged when the panel's Clear button is used
TEAM_STATS_CHANNEL_ID = 1530999622405980334  # the team stats panel is posted/refreshed here
STATS_LOG_CHANNEL_ID = 1531007591331795126  # team stats JSON "database" message lives here
MESSAGE_LOG_CHANNEL_ID = 1530176459229106256       # message-count database backup lives here (tracking is server-wide)

TEAMS_DB_FILE = "teams_data.json"
GIVEAWAYS_DB_FILE = "giveaways_data.json"
MESSAGES_DB_FILE = "messages_data.json"
TEAM_STATS_DB_FILE = "team_stats_data.json"
TICKETS_DB_FILE = "tickets_data.json"
DB_FILE = "teams.json"  # legacy combined file — read only, for one-time migration

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SUPPORT_BANNER_PATH = os.path.join(BASE_DIR, "support_banner.png")
SUPPORT_BANNER_FILENAME = "support_banner.png"


# ---------- Bot setup ----------
intents = discord.Intents.default()
intents.members = True  # needed to reliably resolve members / add roles

bot = commands.Bot(command_prefix="!", intents=intents)


# ---------- JSON "database" helpers ----------
# The team data and giveaway data live in two separate local files (and are backed up to
# two separate Discord channels — see backup_db_to_log_channel / restore_db_from_log_channel
# below), but the rest of the bot still works with a single merged {"teams": ..., "giveaways": ...}
# dict in memory, exactly as before, so nothing else in the file needs to change.
def load_db() -> dict:
    data = {"teams": {}, "giveaways": {}}

    if os.path.exists(TEAMS_DB_FILE):
        with open(TEAMS_DB_FILE, "r", encoding="utf-8") as f:
            teams_file = json.load(f)
        if "teams" not in teams_file:
            # migrate old flat-format {team_name: {...}} files
            teams_file = {"teams": teams_file}
        data["teams"] = teams_file.get("teams", {})

    if os.path.exists(GIVEAWAYS_DB_FILE):
        with open(GIVEAWAYS_DB_FILE, "r", encoding="utf-8") as f:
            giveaways_file = json.load(f)
        data["giveaways"] = giveaways_file.get("giveaways", {})

    return data


def save_db(data: dict) -> None:
    now = discord.utils.utcnow().isoformat()
    with open(TEAMS_DB_FILE, "w", encoding="utf-8") as f:
        json.dump({"teams": data.get("teams", {}), "last_updated": now}, f, indent=2)
    with open(GIVEAWAYS_DB_FILE, "w", encoding="utf-8") as f:
        json.dump({"giveaways": data.get("giveaways", {}), "last_updated": now}, f, indent=2)


# Cache of each database message (keyed by channel ID) so we edit it in place instead of
# posting a new file every time. Populated lazily by scanning channel history.
_db_message_cache: dict = {}

# User IDs with a /createteam request currently awaiting admin confirmation,
# so the same user can't queue up multiple pending requests.
pending_team_requests: set = set()


async def _get_or_create_db_message(channel_id: int, filename: str):
    if channel_id in _db_message_cache:
        return _db_message_cache[channel_id]

    channel = bot.get_channel(channel_id) or await bot.fetch_channel(channel_id)
    async for msg in channel.history(limit=50):
        if msg.author.id == bot.user.id and msg.attachments and msg.attachments[0].filename == filename:
            _db_message_cache[channel_id] = msg
            return msg
    return None


async def _backup_file_to_channel(channel_id: int, file_path: str, filename: str):
    """Keeps a single message in the given channel updated with the given local file,
    editing it in place rather than posting a new file every time."""
    channel = bot.get_channel(channel_id) or await bot.fetch_channel(channel_id)
    with open(file_path, "rb") as f:
        file_bytes = f.read()
    new_file = discord.File(io.BytesIO(file_bytes), filename=filename)

    msg = await _get_or_create_db_message(channel_id, filename)
    if msg is not None:
        try:
            edited = await msg.edit(content="📦 Database (auto-updated):", attachments=[new_file])
            _db_message_cache[channel_id] = edited
            return
        except discord.HTTPException:
            pass  # message may have been deleted; fall through and send a fresh one

    sent = await channel.send(content="📦 Database (auto-updated):", file=new_file)
    _db_message_cache[channel_id] = sent


async def backup_db_to_log_channel():
    """Keeps the teams file backed up in TEAM_LOG_CHANNEL_ID and the giveaways file backed
    up in GIVEAWAY_LOG_CHANNEL_ID, each as its own auto-updated message."""
    await _backup_file_to_channel(TEAM_LOG_CHANNEL_ID, TEAMS_DB_FILE, TEAMS_DB_FILE)
    await _backup_file_to_channel(GIVEAWAY_LOG_CHANNEL_ID, GIVEAWAYS_DB_FILE, GIVEAWAYS_DB_FILE)


async def _restore_file_from_channel(channel_id: int, file_path: str, filename: str) -> bool:
    """Looks for filename attached to one of the bot's own messages in channel_id and, if
    found, writes it to file_path. Returns whether a backup was found."""
    try:
        channel = bot.get_channel(channel_id) or await bot.fetch_channel(channel_id)
        async for msg in channel.history(limit=50):
            if msg.author.id == bot.user.id and msg.attachments and msg.attachments[0].filename == filename:
                data = await msg.attachments[0].read()
                with open(file_path, "wb") as f:
                    f.write(data)
                _db_message_cache[channel_id] = msg
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
    have_teams = os.path.exists(TEAMS_DB_FILE)
    have_giveaways = os.path.exists(GIVEAWAYS_DB_FILE)

    if not have_teams:
        have_teams = await _restore_file_from_channel(TEAM_LOG_CHANNEL_ID, TEAMS_DB_FILE, TEAMS_DB_FILE)
    if not have_giveaways:
        have_giveaways = await _restore_file_from_channel(GIVEAWAY_LOG_CHANNEL_ID, GIVEAWAYS_DB_FILE, GIVEAWAYS_DB_FILE)

    if have_teams and have_giveaways:
        print("Restored database from the team/giveaway log channels.")
        return

    # Fall back to the old combined backup and split it into the two new files.
    legacy = None
    if os.path.exists(DB_FILE):
        with open(DB_FILE, "r", encoding="utf-8") as f:
            legacy = json.load(f)
    else:
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
        with open(TEAMS_DB_FILE, "w", encoding="utf-8") as f:
            json.dump({"teams": legacy.get("teams", {}), "last_updated": migrated_at}, f, indent=2)
        print("Migrated teams data from the legacy combined log channel.")
    if not have_giveaways:
        with open(GIVEAWAYS_DB_FILE, "w", encoding="utf-8") as f:
            json.dump({"giveaways": legacy.get("giveaways", {}), "last_updated": migrated_at}, f, indent=2)
        print("Migrated giveaways data from the legacy combined log channel.")

    # Push the split data into the two new dedicated channels right away, rather than
    # waiting for the next command that happens to save.
    await backup_db_to_log_channel()


# ---------- Message tracking (weekly + overall) ----------
# Every non-bot message sent anywhere in the server is counted per-user, both for the
# current week and all-time. The counts live in MESSAGES_DB_FILE and are backed up as an
# auto-updated message in MESSAGE_LOG_CHANNEL_ID (reusing the generic backup/restore
# helpers above), the same way teams and giveaways are.
#
# Backups are debounced rather than sent on every single message (to stay well clear of
# Discord's edit rate limits on busy servers): each tracked message just flips a "dirty"
# flag, and a short-interval loop below pushes a fresh backup only when something changed.
_message_stats_dirty = False


def load_message_stats() -> dict:
    if not os.path.exists(MESSAGES_DB_FILE):
        return {"users": {}}
    with open(MESSAGES_DB_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    data.setdefault("users", {})
    return data


def save_message_stats(data: dict) -> None:
    data["last_updated"] = discord.utils.utcnow().isoformat()
    with open(MESSAGES_DB_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def _current_week_start() -> str:
    """Returns the ISO date (YYYY-MM-DD) of the Monday 00:00 UTC that starts the current week."""
    now = discord.utils.utcnow()
    monday = now - timedelta(days=now.weekday())
    return monday.date().isoformat()


def record_tracked_message(user_id: int) -> None:
    global _message_stats_dirty
    data = load_message_stats()
    week_start = _current_week_start()
    key = str(user_id)
    entry = data["users"].setdefault(key, {"overall": 0, "weekly": 0, "week_start": week_start})
    if entry.get("week_start") != week_start:
        # A new week has started since this user's last tracked message — reset the weekly count.
        entry["weekly"] = 0
        entry["week_start"] = week_start
    entry["weekly"] = entry.get("weekly", 0) + 1
    entry["overall"] = entry.get("overall", 0) + 1
    save_message_stats(data)
    _message_stats_dirty = True


async def backup_message_stats_to_log_channel():
    if not os.path.exists(MESSAGES_DB_FILE):
        return
    try:
        await _backup_file_to_channel(MESSAGE_LOG_CHANNEL_ID, MESSAGES_DB_FILE, MESSAGES_DB_FILE)
    except discord.HTTPException as e:
        print(f"Failed to back up message stats to log channel: {e}")
    except Exception as e:  # noqa: BLE001 - never let a bad backup attempt kill the loop
        print(f"Unexpected error backing up message stats: {e}")


async def restore_message_stats_from_log_channel():
    if os.path.exists(MESSAGES_DB_FILE):
        # Local data already present (e.g. a crash-restart, not a fresh container) — push
        # it straight to the log channel so the backup there is confirmed up to date.
        await backup_message_stats_to_log_channel()
        return
    try:
        found = await _restore_file_from_channel(MESSAGE_LOG_CHANNEL_ID, MESSAGES_DB_FILE, MESSAGES_DB_FILE)
    except discord.HTTPException as e:
        print(f"Failed to restore message stats from log channel: {e}")
        return
    if found:
        print("Restored message stats from log channel backup.")
    else:
        print("No existing message stats backup found — starting fresh.")


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return
    if message.guild is None:
        return  # ignore DMs
    record_tracked_message(message.author.id)


@tasks.loop(seconds=30)
async def backup_message_stats():
    global _message_stats_dirty
    if not _message_stats_dirty:
        return
    _message_stats_dirty = False
    await backup_message_stats_to_log_channel()


@backup_message_stats.before_loop
async def before_backup_message_stats():
    await bot.wait_until_ready()


# ---------- Team stats (set via /statssettings, shown on the team-stats panel) ----------
# Match/performance stats aren't derived from anything else the bot tracks — they're set
# manually by staff via /statssettings and just stored per-team here. Keyed by team name so
# a rename in /changeteamsettings or /staffchangesetting doesn't automatically carry the
# stats over; see the rename-handling note near those commands.
TEAM_STAT_FIELDS = {
    "rating": "Overall Rating",
    "matches": "Matches ever played",
    "tournaments": "Tournaments Won",
    "placement": "Best ever placement",
    "winrate": "Win Rate",
    "avgkills": "Average kills",
    "avgdeaths": "Average deaths",
    "kd": "Kill to death Ratio",
}


def load_team_stats() -> dict:
    if not os.path.exists(TEAM_STATS_DB_FILE):
        return {"teams": {}}
    with open(TEAM_STATS_DB_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    data.setdefault("teams", {})
    return data


def save_team_stats(data: dict) -> None:
    data["last_updated"] = discord.utils.utcnow().isoformat()
    with open(TEAM_STATS_DB_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


async def backup_team_stats_to_log_channel():
    try:
        await _backup_file_to_channel(STATS_LOG_CHANNEL_ID, TEAM_STATS_DB_FILE, TEAM_STATS_DB_FILE)
    except discord.HTTPException as e:
        print(f"Failed to back up team stats to log channel: {e}")
    except Exception as e:  # noqa: BLE001 - never let a bad backup attempt kill a command
        print(f"Unexpected error backing up team stats: {e}")


async def restore_team_stats_from_log_channel():
    if os.path.exists(TEAM_STATS_DB_FILE):
        # Local data already present (e.g. a crash-restart, not a fresh container) — push
        # it straight to the log channel so the backup there is confirmed up to date.
        await backup_team_stats_to_log_channel()
        return
    try:
        found = await _restore_file_from_channel(STATS_LOG_CHANNEL_ID, TEAM_STATS_DB_FILE, TEAM_STATS_DB_FILE)
    except discord.HTTPException as e:
        print(f"Failed to restore team stats from log channel: {e}")
        return
    if found:
        print("Restored team stats from log channel backup.")
    else:
        print("No existing team stats backup found — starting fresh.")


# ---------- Tickets (opened from the support panel) ----------
# Each opened ticket gets a sequential number and a thread; the counter and a record of
# every ticket (thread ID -> number/opener/category/closed state) live in TICKETS_DB_FILE
# and are backed up as an auto-updated message in TICKET_LOG_CHANNEL_ID, the same way
# teams/giveaways/message-stats/team-stats are.
def load_ticket_db() -> dict:
    if not os.path.exists(TICKETS_DB_FILE):
        return {"next_number": 1, "tickets": {}}
    with open(TICKETS_DB_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    data.setdefault("next_number", 1)
    data.setdefault("tickets", {})
    return data


def save_ticket_db(data: dict) -> None:
    data["last_updated"] = discord.utils.utcnow().isoformat()
    with open(TICKETS_DB_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


async def backup_ticket_db_to_log_channel():
    try:
        await _backup_file_to_channel(TICKET_LOG_CHANNEL_ID, TICKETS_DB_FILE, TICKETS_DB_FILE)
    except discord.HTTPException as e:
        print(f"Failed to back up ticket db to log channel: {e}")
    except Exception as e:  # noqa: BLE001 - never let a bad backup attempt kill a command
        print(f"Unexpected error backing up ticket db: {e}")


async def restore_ticket_db_from_log_channel():
    if os.path.exists(TICKETS_DB_FILE):
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


def find_team_by_leader(db: dict, user_id: int):
    for name, info in db.items():
        if info["leader_id"] == user_id:
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


def has_create_team_access(member: discord.Member) -> bool:
    return any(role.id == CREATE_TEAM_ROLE_ID for role in member.roles)


def team_leader_channel_overwrite() -> discord.PermissionOverwrite:
    """Permissions granted to a team leader in their own team channel: on top of viewing/
    sending, manage_messages lets them delete and pin messages, and mention_everyone lets
    them ping their team's role even though the role itself isn't set to be mentionable."""
    return discord.PermissionOverwrite(
        view_channel=True,
        send_messages=True,
        manage_messages=True,
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


def build_support_ticket_embed() -> discord.Embed:
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
    embed = discord.Embed(
        title=SUPPORT_PANEL_TITLE,
        description=description,
        colour=discord.Colour.orange(),
    )
    embed.set_image(url=f"attachment://{SUPPORT_BANNER_FILENAME}")
    embed.set_footer(text="Animal Company: Arena Hub")
    return embed


# ---------- Persistent "Close" button attached to every ticket thread's first message ----------
class TicketCloseView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Close", emoji="🔒", style=discord.ButtonStyle.danger, custom_id="ticket_close_button")
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button):
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

        for child in self.children:
            child.disabled = True
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


# ---------- Dropdown shown under the support ticket panel banner — opens a ticket thread ----------
async def _create_ticket_thread(
    interaction: discord.Interaction, category_label: str, answers: list[tuple[str, str]]
):
    """Shared by the intake modal: opens the ticket thread, pings the role + opener, posts
    an embed with the submitted answers, and logs the ticket to the database. `answers` is
    a list of (question, response) pairs shown in the ticket embed."""
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

    description = f"**Category:** {category_label}\n\n"
    description += "\n\n".join(f"**{question}**\n{response}" for question, response in answers)
    embed = discord.Embed(title=f"Ticket #{number}", description=description, colour=discord.Colour.orange())
    embed.set_footer(text=f"Opened by {interaction.user}")

    await thread.send(
        content=f"<@&{TICKET_PING_ROLE_ID}> {interaction.user.mention}",
        embed=embed,
        view=TicketCloseView(),
    )

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


class SupportPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.select(
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
    async def category_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        await interaction.response.send_modal(TicketIntakeModal(select.values[0]))


async def refresh_support_ticket_panel():
    """Posts the support ticket panel if one isn't already up in the target channel.
    Unlike before, this does NOT delete and repost the panel on every startup — that would
    spam the channel. It only posts a fresh panel the first time (or if the old one was
    deleted)."""
    channel = bot.get_channel(SUPPORT_TICKET_CHANNEL_ID) or await bot.fetch_channel(SUPPORT_TICKET_CHANNEL_ID)

    async for msg in channel.history(limit=50):
        if msg.author.id == bot.user.id and msg.embeds and msg.embeds[0].title == SUPPORT_PANEL_TITLE:
            return  # panel already posted — leave it alone

    view = SupportPanelView()

    if not os.path.exists(SUPPORT_BANNER_PATH):
        print(f"Support banner image missing at {SUPPORT_BANNER_PATH} — panel sent without image.")
        await channel.send(embed=build_support_ticket_embed(), view=view)
        return

    embed = build_support_ticket_embed()
    file = discord.File(SUPPORT_BANNER_PATH, filename=SUPPORT_BANNER_FILENAME)
    await channel.send(embed=embed, file=file, view=view)


# ---------- Team stats panel ----------
TEAM_STATS_PANEL_TITLE = "AC: Arena Hub"

# ---------- Tournament submission panel ----------
TOURNAMENT_PANEL_TITLE = "Tournament Submissions"
TOURNAMENT_COMPETITOR_EMOJI = "<:SilverTrophy:1528216893297791098>"
TOURNAMENT_SUB_EMOJI = "<:Revolver:1528216974973210747>"

# Matches a slot line like "`1.` " (empty) or "`1.` <@123456789012345678>" (filled)
_TOURNAMENT_SLOT_LINE_RE = re.compile(r"^`(\d+)\.`\s*(?:<@!?(\d+)>)?\s*$")


def build_tournament_submission_content(
    competitor_count: int, sub_count: int, competitors: list = None, subs: list = None
) -> str:
    """Builds the '**Tournament Submission**' message body. `competitors`/`subs` are lists of
    user IDs (or None for an empty slot); if omitted, all slots start empty."""
    competitors = list(competitors) if competitors is not None else [None] * competitor_count
    subs = list(subs) if subs is not None else [None] * sub_count

    lines = ["**Tournament Submission**", f"{TOURNAMENT_COMPETITOR_EMOJI} Competitors :"]
    for i in range(competitor_count):
        filler = f"<@{competitors[i]}>" if i < len(competitors) and competitors[i] else ""
        lines.append(f"`{i + 1}.` {filler}".rstrip())

    lines.append(f"{TOURNAMENT_SUB_EMOJI}  Subs :")
    for i in range(sub_count):
        filler = f"<@{subs[i]}>" if i < len(subs) and subs[i] else ""
        lines.append(f"`{i + 1}.` {filler}".rstrip())

    return "\n".join(lines)


def parse_tournament_submission_content(content: str):
    """Reads a tournament submission message back into (competitor_ids, sub_ids) lists,
    where each entry is a user ID or None for an empty slot."""
    competitors, subs = [], []
    section = None
    for line in content.split("\n"):
        if line.startswith(TOURNAMENT_COMPETITOR_EMOJI):
            section = "competitors"
            continue
        if line.startswith(TOURNAMENT_SUB_EMOJI):
            section = "subs"
            continue
        match = _TOURNAMENT_SLOT_LINE_RE.match(line)
        if not match:
            continue
        user_id = int(match.group(2)) if match.group(2) else None
        if section == "competitors":
            competitors.append(user_id)
        elif section == "subs":
            subs.append(user_id)
    return competitors, subs


class TournamentSubmissionView(discord.ui.View):
    """Attached to each '**Tournament Submission**' message. Reads/writes its state straight
    from the message content, so it works for any number of these messages with one
    persistent, restart-proof view."""

    def __init__(self):
        super().__init__(timeout=None)

    async def _update_signup(self, interaction: discord.Interaction, target: str):
        message = interaction.message
        competitors, subs = parse_tournament_submission_content(message.content)
        user_id = interaction.user.id
        in_competitors = user_id in competitors
        in_subs = user_id in subs

        if target == "remove":
            if not in_competitors and not in_subs:
                await interaction.response.send_message(
                    "You're not currently signed up on this sheet.", ephemeral=True
                )
                return
            if in_competitors:
                competitors[competitors.index(user_id)] = None
            if in_subs:
                subs[subs.index(user_id)] = None
        else:
            target_list = competitors if target == "competitors" else subs
            label = "competitor" if target == "competitors" else "sub"

            if user_id in target_list:
                await interaction.response.send_message(
                    f"You're already signed up as a {label}.", ephemeral=True
                )
                return
            if None not in target_list:
                await interaction.response.send_message(
                    f"There are no open {label} slots.", ephemeral=True
                )
                return

            # moving from the other list, if they were on it
            if in_competitors:
                competitors[competitors.index(user_id)] = None
            if in_subs:
                subs[subs.index(user_id)] = None

            target_list[target_list.index(None)] = user_id

        new_content = build_tournament_submission_content(len(competitors), len(subs), competitors, subs)
        await interaction.response.edit_message(content=new_content)

    @discord.ui.button(
        label="Competitors", style=discord.ButtonStyle.primary, custom_id="tournament_submission_competitors"
    )
    async def competitors_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._update_signup(interaction, "competitors")

    @discord.ui.button(
        label="Subs", style=discord.ButtonStyle.primary, custom_id="tournament_submission_subs"
    )
    async def subs_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._update_signup(interaction, "subs")

    @discord.ui.button(
        label="Remove", style=discord.ButtonStyle.danger, custom_id="tournament_submission_remove"
    )
    async def remove_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._update_signup(interaction, "remove")

    @discord.ui.button(
        label="Submit", style=discord.ButtonStyle.success, custom_id="tournament_submission_submit"
    )
    async def submit_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        db = load_db()
        team_key = find_team_by_channel(db["teams"], interaction.channel_id)
        if not team_key:
            await interaction.response.send_message(
                "Couldn't figure out which team this submission sheet belongs to.", ephemeral=True
            )
            return

        info = db["teams"][team_key]
        if interaction.user.id != info.get("leader_id"):
            await interaction.response.send_message(
                "Only the team leader can submit this.", ephemeral=True
            )
            return

        competitors, subs = parse_tournament_submission_content(interaction.message.content)
        recipient_ids = [uid for uid in (competitors + subs) if uid is not None]
        if not recipient_ids:
            await interaction.response.send_message(
                "Nobody has signed up yet — nothing to submit.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)

        guild = interaction.guild
        role = guild.get_role(TOURNAMENT_SUBMISSION_ROLE_ID)

        granted = 0
        failed_ids = []
        for user_id in recipient_ids:
            member = guild.get_member(user_id)
            if member is None:
                try:
                    member = await guild.fetch_member(user_id)
                except discord.HTTPException:
                    member = None
            if member is None or role is None:
                failed_ids.append(user_id)
                continue
            try:
                await member.add_roles(role, reason=f"Tournament submission by {interaction.user} ({team_key})")
                granted += 1
            except discord.HTTPException:
                failed_ids.append(user_id)

        try:
            await interaction.message.delete()
        except discord.HTTPException:
            pass

        if role is None:
            result_message = "⚠️ Submitted, but couldn't find the tournament role to assign — check the role ID."
        else:
            result_message = f"✅ Submitted — gave the tournament role to {granted} member(s)."
            if failed_ids:
                result_message += f" Couldn't update {len(failed_ids)} member(s)."

        await interaction.followup.send(result_message, ephemeral=True)


class TournamentSubmissionModal(discord.ui.Modal):
    def __init__(self, team_name: str):
        super().__init__(title=f"Tournament Submission — {team_name}"[:45])
        self.team_name = team_name
        self.competitors_input = discord.ui.TextInput(
            label="How much competitors?", placeholder="e.g. 5", max_length=3
        )
        self.backups_input = discord.ui.TextInput(
            label="How much backups?", placeholder="e.g. 2", max_length=3
        )
        self.add_item(self.competitors_input)
        self.add_item(self.backups_input)

    async def on_submit(self, interaction: discord.Interaction):
        competitors_raw = self.competitors_input.value.strip()
        backups_raw = self.backups_input.value.strip()

        if not competitors_raw.isdigit() or not backups_raw.isdigit():
            await interaction.response.send_message(
                "Both fields need to be whole numbers.", ephemeral=True
            )
            return

        competitor_count = int(competitors_raw)
        backup_count = int(backups_raw)

        if not (1 <= competitor_count <= 50) or not (0 <= backup_count <= 50):
            await interaction.response.send_message(
                "Use a competitor count between 1–50 and a backup count between 0–50.", ephemeral=True
            )
            return

        db = load_db()
        info = db["teams"].get(self.team_name)
        if info is None:
            await interaction.response.send_message(
                "That team no longer exists — the panel may be out of date.", ephemeral=True
            )
            return

        channel = interaction.guild.get_channel(info["channel_id"])
        if channel is None:
            await interaction.response.send_message(
                "That team's channel no longer exists.", ephemeral=True
            )
            return

        content = build_tournament_submission_content(competitor_count, backup_count)
        await channel.send(content=content, view=TournamentSubmissionView())

        await interaction.response.send_message(
            f"Tournament submission sheet posted in {channel.mention}.", ephemeral=True
        )



TOURNAMENT_TEAMS_PER_PAGE = 25
_TOURNAMENT_PAGE_RE = re.compile(r"page (\d+)/(\d+)")


class TournamentTeamSelectView(discord.ui.View):
    """The dropdown panel itself. Discord caps select menus at 25 options, so teams are
    split across pages of 25 with Prev/Next buttons once there are more than that.

    Current page isn't kept on the view instance — it's read back from the live message's
    select placeholder (e.g. "... (page 2/3)") whenever Prev/Next is pressed, since a
    persistent view's registered instance is shared across every message using it and
    can't hold per-message state that survives a restart."""

    def __init__(self, team_names: list = None, page: int = 0, keep_nav_buttons: bool = False):
        super().__init__(timeout=None)
        all_names = list(team_names or [])
        total_pages = max(1, -(-len(all_names) // TOURNAMENT_TEAMS_PER_PAGE)) if all_names else 1
        page = max(0, min(page, total_pages - 1))
        start = page * TOURNAMENT_TEAMS_PER_PAGE
        page_names = all_names[start:start + TOURNAMENT_TEAMS_PER_PAGE]

        options = [discord.SelectOption(label=name[:100], value=name[:100]) for name in page_names]
        if not options:
            options = [discord.SelectOption(label="No teams yet", value="__none__")]
        self.team_select.options = options

        placeholder = "Select a team..."
        if total_pages > 1:
            placeholder += f" (page {page + 1}/{total_pages})"
        self.team_select.placeholder = placeholder

        if total_pages <= 1 and not keep_nav_buttons:
            self.remove_item(self.prev_page)
            self.remove_item(self.next_page)
        else:
            self.prev_page.disabled = page <= 0
            self.next_page.disabled = page >= total_pages - 1

    @discord.ui.select(
        placeholder="Select a team...",
        custom_id="tournament_team_select",
        options=[discord.SelectOption(label="placeholder", value="placeholder")],
    )
    async def team_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        team_name = select.values[0]
        if team_name == "__none__":
            await interaction.response.send_message("There are no teams yet.", ephemeral=True)
            return

        db = load_db()
        if team_name not in db["teams"]:
            await interaction.response.send_message(
                "That team no longer exists — the panel may be out of date.", ephemeral=True
            )
            return

        await interaction.response.send_modal(TournamentSubmissionModal(team_name))

    @discord.ui.button(label="◀ Prev", style=discord.ButtonStyle.secondary, custom_id="tournament_team_prev_page", row=1)
    async def prev_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._go_to_page(interaction, -1)

    @discord.ui.button(label="Next ▶", style=discord.ButtonStyle.secondary, custom_id="tournament_team_next_page", row=1)
    async def next_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._go_to_page(interaction, 1)

    async def _go_to_page(self, interaction: discord.Interaction, delta: int):
        current_page = 0
        for row in interaction.message.components:
            for component in row.children:
                if getattr(component, "custom_id", None) == "tournament_team_select":
                    match = _TOURNAMENT_PAGE_RE.search(component.placeholder or "")
                    if match:
                        current_page = int(match.group(1)) - 1

        db = load_db()
        team_names = sorted(db["teams"].keys())
        new_view = TournamentTeamSelectView(team_names, page=current_page + delta)
        await interaction.response.edit_message(view=new_view)

    @discord.ui.button(
        label="Clear", style=discord.ButtonStyle.danger, custom_id="tournament_clear_button", row=2
    )
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
            f"🧹 Cleared — removed {role_note} from {removed} member(s) and purged "
            f"{purged} message(s) from {channel_mention}.",
            ephemeral=True,
        )


async def refresh_tournament_panel():
    """Posts the tournament team-select panel if one isn't already up in the target
    channel. Unlike before, this does NOT delete and repost the panel on every startup —
    that would drop the persistent Clear button's state and spam the channel. It only
    posts a fresh panel the first time (or if the old one was deleted)."""
    channel = bot.get_channel(TOURNAMENT_PANEL_CHANNEL_ID) or await bot.fetch_channel(TOURNAMENT_PANEL_CHANNEL_ID)

    async for msg in channel.history(limit=50):
        if msg.author.id == bot.user.id and msg.embeds and msg.embeds[0].title == TOURNAMENT_PANEL_TITLE:
            return  # panel already posted — leave it alone

    db = load_db()
    team_names = sorted(db["teams"].keys())

    embed = discord.Embed(
        title=TOURNAMENT_PANEL_TITLE,
        description=(
            "Select your team below to submit your competitors and backups for the tournament.\n"
            "Use Prev/Next to page through teams if there are more than 25."
        ),
        colour=discord.Colour.gold(),
    )
    await channel.send(embed=embed, view=TournamentTeamSelectView(team_names))


TEAM_STATS_TEAMS_PER_PAGE = 25
_TEAM_STATS_PAGE_RE = re.compile(r"page (\d+)/(\d+)")


def build_team_stats_embed(team_name: str, info: dict, stats: dict, guild: discord.Guild) -> discord.Embed:
    """Stats card shown when someone picks a team from the stats dropdown. `stats` comes
    from the team_stats database (set via /statssettings) — any field not yet set shows as
    N/A. The embed colour follows the team's own role colour instead of a fixed colour."""
    role = guild.get_role(info.get("role_id"))
    leader_id = info.get("leader_id")
    emoji = info.get("emoji", "")

    def stat(key: str) -> str:
        value = stats.get(key)
        return str(value) if value not in (None, "") else "N/A"

    description = (
        f"## {team_name} Team {emoji}\n"
        f"Leader: {f'<@{leader_id}>' if leader_id else 'Unknown'}\n"
        f"Overall Rating: ` {stat('rating')} `\n"
        f"---\n"
        f"*Matches ever played:* `{stat('matches')}`\n"
        f"*Tournaments Won:* ` {stat('tournaments')} `\n"
        f"*Best ever placement:* ` {stat('placement')} `\n"
        f"---\n"
        f"*Win Rate:* ` {stat('winrate')} `\n"
        f"*Average kills:* ` {stat('avgkills')} `\n"
        f"*Average deaths:* ` {stat('avgdeaths')} `\n"
        f"*Kill to death Ratio:* ` {stat('kd')} `"
    )

    embed = discord.Embed(
        description=description,
        colour=role.colour if role is not None else discord.Colour.orange(),
    )
    return embed


class TeamStatsSelectView(discord.ui.View):
    """The dropdown panel shown in the team-stats channel. Same paging approach as
    TournamentTeamSelectView (Discord caps select menus at 25 options, and current page is
    read back from the live message's select placeholder so a single persistent view
    instance can serve every message and survive restarts), but each option also shows the
    team's emoji, and picking one replies with that team's stats instead of opening a modal."""

    def __init__(self, team_names: list = None, page: int = 0, keep_nav_buttons: bool = False):
        super().__init__(timeout=None)
        db = load_db()
        all_names = list(team_names or [])
        total_pages = max(1, -(-len(all_names) // TEAM_STATS_TEAMS_PER_PAGE)) if all_names else 1
        page = max(0, min(page, total_pages - 1))
        start = page * TEAM_STATS_TEAMS_PER_PAGE
        page_names = all_names[start:start + TEAM_STATS_TEAMS_PER_PAGE]

        options = []
        for name in page_names:
            info = db["teams"].get(name, {})
            emoji = info.get("emoji")
            option_kwargs = {"label": name[:100], "value": name[:100]}
            if emoji and is_valid_standard_emoji(emoji):
                option_kwargs["emoji"] = emoji
            options.append(discord.SelectOption(**option_kwargs))
        if not options:
            options = [discord.SelectOption(label="No teams yet", value="__none__")]
        self.team_select.options = options

        placeholder = "Select a team..."
        if total_pages > 1:
            placeholder += f" (page {page + 1}/{total_pages})"
        self.team_select.placeholder = placeholder

        if total_pages <= 1 and not keep_nav_buttons:
            self.remove_item(self.prev_page)
            self.remove_item(self.next_page)
        else:
            self.prev_page.disabled = page <= 0
            self.next_page.disabled = page >= total_pages - 1

    @discord.ui.select(
        placeholder="Select a team...",
        custom_id="team_stats_select",
        options=[discord.SelectOption(label="placeholder", value="placeholder")],
    )
    async def team_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        team_name = select.values[0]
        if team_name == "__none__":
            await interaction.response.send_message("There are no teams yet.", ephemeral=True)
            return

        db = load_db()
        info = db["teams"].get(team_name)
        if info is None:
            await interaction.response.send_message(
                "That team no longer exists — the panel may be out of date.", ephemeral=True
            )
            return

        stats_db = load_team_stats()
        stats = stats_db["teams"].get(team_name, {})

        embed = build_team_stats_embed(team_name, info, stats, interaction.guild)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(label="◀ Prev", style=discord.ButtonStyle.secondary, custom_id="team_stats_prev_page", row=1)
    async def prev_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._go_to_page(interaction, -1)

    @discord.ui.button(label="Next ▶", style=discord.ButtonStyle.secondary, custom_id="team_stats_next_page", row=1)
    async def next_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._go_to_page(interaction, 1)

    async def _go_to_page(self, interaction: discord.Interaction, delta: int):
        current_page = 0
        for row in interaction.message.components:
            for component in row.children:
                if getattr(component, "custom_id", None) == "team_stats_select":
                    match = _TEAM_STATS_PAGE_RE.search(component.placeholder or "")
                    if match:
                        current_page = int(match.group(1)) - 1

        db = load_db()
        team_names = sorted(db["teams"].keys())
        new_view = TeamStatsSelectView(team_names, page=current_page + delta)
        await interaction.response.edit_message(view=new_view)


async def refresh_team_stats_panel():
    """Deletes any previously posted team-stats panel in the target channel and posts a
    fresh one listing the current teams. Called on every bot startup so the panel never
    goes stale or duplicates across restarts."""
    channel = bot.get_channel(TEAM_STATS_CHANNEL_ID) or await bot.fetch_channel(TEAM_STATS_CHANNEL_ID)

    async for msg in channel.history(limit=50):
        if msg.author.id == bot.user.id and msg.embeds and msg.embeds[0].title == TEAM_STATS_PANEL_TITLE:
            try:
                await msg.delete()
            except discord.HTTPException:
                pass

    db = load_db()
    team_names = sorted(db["teams"].keys())

    embed = discord.Embed(
        title=TEAM_STATS_PANEL_TITLE,
        description=(
            "Welcome! Here, you'll find stats for every team including your own. "
            "Please keep in mind, these stats may not be full accurate."
        ),
        colour=discord.Colour.orange(),
    )
    embed.set_image(url=f"attachment://{SUPPORT_BANNER_FILENAME}")

    view = TeamStatsSelectView(team_names)

    if os.path.exists(SUPPORT_BANNER_PATH):
        file = discord.File(SUPPORT_BANNER_PATH, filename=SUPPORT_BANNER_FILENAME)
        await channel.send(embed=embed, file=file, view=view)
    else:
        embed.set_image(url=None)
        await channel.send(embed=embed, view=view)


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

    save_db(db)
    await backup_db_to_log_channel()
    return True


async def sync_existing_teams():
    """Backfill pass run on every startup: makes sure every current team leader holds
    TEAM_LEADER_ROLE_ID and has the manage-messages/mention-everyone overrides in their
    own team channel (so they can ping the team, delete messages, and pin messages).
    Idempotent — cheap after the first run, and self-heals if a permission or role is
    ever reverted manually."""
    db = load_db()
    if not db["teams"]:
        return

    guild = None
    leader_role_granted = 0
    perms_updated = 0

    for team_name, info in db["teams"].items():
        leader_id = info.get("leader_id")
        channel_id = info.get("channel_id")
        if leader_id is None:
            continue

        try:
            if guild is None:
                # all teams live in one guild for this bot; grab it from any known channel
                seed_channel = bot.get_channel(channel_id) or await bot.fetch_channel(channel_id)
                guild = seed_channel.guild
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

        channel = guild.get_channel(channel_id)
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

    if leader_role_granted or perms_updated:
        print(
            f"Backfilled team-leader role onto {leader_role_granted} leader(s) and "
            f"channel permissions onto {perms_updated} leader(s)."
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
            await interaction.edit_original_response(content="That team no longer exists.", view=None)
            return

        for child in self.children:
            child.disabled = True
        await interaction.edit_original_response(
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
            await interaction.edit_original_response(content="That team no longer exists.", view=self)
            return
        await interaction.edit_original_response(
            content=f"🗑️ Team **{self.team_name}** has been deleted.", view=self
        )

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(content="Cancelled — team not deleted.", view=self)


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
        category = guild.get_channel(TEAM_CATEGORY_ID)

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

        channel_name = f"{self.emoji}┃{self.team_name}-Team"
        team_channel = await guild.create_text_channel(
            name=channel_name,
            category=category,
            overwrites=overwrites,
            reason=f"Team created, confirmed by {interaction.user}",
        )

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

        db = load_db()
        db["teams"][self.team_name] = {
            "emoji": self.emoji,
            "leader_id": self.requester_id,
            "role_id": role.id,
            "channel_id": team_channel.id,
            "members": [self.requester_id],
        }
        record_team_join(db["teams"][self.team_name], self.requester_id)
        save_db(db)
        await backup_db_to_log_channel()
        pending_team_requests.discard(self.requester_id)

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
        await interaction.followup.send("Team creation denied.", ephemeral=True)


# ---------- Invite response view (DM'd to the invited user) ----------
class InviteResponseView(discord.ui.View):
    def __init__(self, team_name: str, invited_user_id: int, guild_id: int):
        super().__init__(timeout=86400)  # 24h to respond
        self.team_name = team_name
        self.invited_user_id = invited_user_id
        self.guild_id = guild_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.invited_user_id:
            await interaction.response.send_message("This invite isn't for you.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Yes", style=discord.ButtonStyle.success)
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()

        db = load_db()
        info = db["teams"].get(self.team_name)
        if info is None:
            for child in self.children:
                child.disabled = True
            await interaction.edit_original_response(content="This team no longer exists.", view=self)
            return

        if (
            self.invited_user_id not in info.get("members", [])
            and len(info.get("members", [])) >= MAX_TEAM_MEMBERS
        ):
            for child in self.children:
                child.disabled = True
            await interaction.edit_original_response(
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

        channel = guild.get_channel(info["channel_id"])
        if channel:
            await channel.send(f"🎉 {member.mention} just joined the team!")

        for child in self.children:
            child.disabled = True
        await interaction.edit_original_response(content=f"You joined **{self.team_name}**! 🎉", view=self)

    @discord.ui.button(label="No", style=discord.ButtonStyle.danger)
    async def decline(self, interaction: discord.Interaction, button: discord.ui.Button):
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(content="Invite declined.", view=self)


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


def build_giveaway_embed(
    prize: str,
    winners_count: int,
    host_id: int,
    end_ts: int,
    entries_count: int,
    winner_ids: list = None,
) -> discord.Embed:
    """Mirrors the AC: Arena Hub giveaway style: orange embed, a timestamp for when it
    ends (or ended), the host, a live entry count, and — once it's over — the winners."""
    ended = winner_ids is not None

    embed = discord.Embed(
        title=f"{GIVEAWAY_JOIN_EMOJI} {prize}",
        colour=discord.Colour.orange(),
    )

    if ended:
        embed.add_field(name="Ended", value=f"<t:{end_ts}:F>", inline=False)
    else:
        embed.add_field(name="Ends", value=f"<t:{end_ts}:R> (<t:{end_ts}:F>)", inline=False)

    embed.add_field(name="Hosted by", value=f"<@{host_id}>", inline=False)
    embed.add_field(name="Entries", value=f"**{entries_count}**", inline=False)

    if ended:
        winners_value = (
            ", ".join(f"<@{uid}>" for uid in winner_ids) if winner_ids else "No valid entries"
        )
        embed.add_field(name="Winners" if len(winner_ids) != 1 else "Winner", value=winners_value, inline=False)
    else:
        embed.set_footer(
            text=f"{winners_count} winner(s) • Click {GIVEAWAY_JOIN_EMOJI} Join below to enter!"
        )

    embed.set_image(url=f"attachment://{SUPPORT_BANNER_FILENAME}")
    return embed


class GiveawayJoinView(discord.ui.View):
    """Persistent green 'Join' button attached to every giveaway message. Entries are kept
    in the database keyed by message ID (not on the view instance, since one registered
    view instance backs every giveaway message and must survive restarts)."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Join", emoji=GIVEAWAY_JOIN_EMOJI, style=discord.ButtonStyle.success,
        custom_id="giveaway_join_button",
    )
    async def join(self, interaction: discord.Interaction, button: discord.ui.Button):
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
        # Persist every entry/leave to the log-channel backup immediately, not just when a
        # giveaway ends — otherwise entries collected since the last backup would be lost
        # if the bot restarts (Railway wipes the container disk on every redeploy).
        await backup_db_to_log_channel()

        embed = build_giveaway_embed(
            prize=info["prize"],
            winners_count=info["winners"],
            host_id=info["host_id"],
            end_ts=info["end_ts"],
            entries_count=len(entries),
        )
        try:
            await interaction.response.edit_message(embed=embed)
        except discord.HTTPException:
            pass

        if joined:
            await interaction.followup.send(f"{GIVEAWAY_JOIN_EMOJI} You're in — good luck!", ephemeral=True)
        else:
            await interaction.followup.send("You left the giveaway.", ephemeral=True)


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

    embed = build_giveaway_embed(
        prize=info["prize"],
        winners_count=info["winners"],
        host_id=info["host_id"],
        end_ts=info["end_ts"],
        entries_count=len(entries),
        winner_ids=winner_ids,
    )

    ended_view = GiveawayJoinView()
    for child in ended_view.children:
        child.disabled = True

    try:
        message = await channel.fetch_message(int(message_id))
        await message.edit(embed=embed, view=ended_view)
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

    if changed:
        save_db(db)
        await backup_db_to_log_channel()


@check_giveaways.before_loop
async def before_check_giveaways():
    await bot.wait_until_ready()


# ---------- Slash commands ----------
@bot.tree.command(name="createteam", description="Create a new team")
@app_commands.describe(
    name="Team name",
    emoji="A single standard Discord emoji for the team (no custom server emojis)",
    colour="Hex colour for the team's role, e.g. #5865F2",
)
async def createteam(interaction: discord.Interaction, name: str, emoji: str, colour: str):
    await interaction.response.defer(ephemeral=True)

    if not has_create_team_access(interaction.user):
        await interaction.followup.send(
            "You must be level 5 to create a team.", ephemeral=True
        )
        return

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

    members = sorted(role.members, key=lambda m: m.id != info["leader_id"])
    lines = [
        member.mention + (" (Leader)" if member.id == info["leader_id"] else "")
        for member in members
    ]
    embed = discord.Embed(
        title=f"{info['emoji']} {key} Team",
        description="\n".join(lines) if lines else "No members with this role yet.",
    )
    await interaction.followup.send(embed=embed)


@teammembers.autocomplete("team")
async def teammembers_team_autocomplete(interaction: discord.Interaction, current: str):
    db = load_db()
    return [
        app_commands.Choice(name=key, value=key)
        for key in db["teams"].keys()
        if current.lower() in key.lower()
    ][:25]


@bot.tree.command(name="invite", description="Invite a user to your team")
@app_commands.describe(user="The user to invite")
async def invite(interaction: discord.Interaction, user: discord.Member):
    await interaction.response.defer(ephemeral=True)

    db = load_db()
    team_key = find_team_by_leader(db["teams"], interaction.user.id)
    if not team_key:
        await interaction.followup.send("You must be a team leader to invite people.", ephemeral=True)
        return

    if user.bot:
        await interaction.followup.send("You can't invite bots.", ephemeral=True)
        return

    if find_team_by_member(db["teams"], user.id):
        await interaction.followup.send("That user is already on a team.", ephemeral=True)
        return

    info = db["teams"][team_key]

    if len(info.get("members", [])) >= MAX_TEAM_MEMBERS:
        await interaction.followup.send(
            f"**{team_key}** is already at the {MAX_TEAM_MEMBERS}-member cap — remove someone first.",
            ephemeral=True,
        )
        return

    view = InviteResponseView(team_key, user.id, interaction.guild.id)
    try:
        await user.send(
            f"{interaction.user.mention} invited you to join **{team_key}** {info['emoji']}! "
            f"Would you like to join?",
            view=view,
        )
    except discord.Forbidden:
        await interaction.followup.send(
            "Couldn't DM that user (they may have DMs off).", ephemeral=True
        )
        return

    await interaction.followup.send(f"Invite sent to {user.mention}.", ephemeral=True)


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

    info["members"] = [uid for uid in info["members"] if uid != interaction.user.id]
    clear_team_join(info, interaction.user.id)
    save_db(db)
    await backup_db_to_log_channel()

    await interaction.followup.send(f"You left **{team_key}**.", ephemeral=True)


@bot.tree.command(name="kickteammember", description="Remove a member from your team")
@app_commands.describe(member="The team member to remove")
async def kickteammember(interaction: discord.Interaction, member: discord.Member):
    await interaction.response.defer(ephemeral=True)

    db = load_db()
    team_key = find_team_by_leader(db["teams"], interaction.user.id)
    if not team_key:
        await interaction.followup.send("You must be a team leader to use this command.", ephemeral=True)
        return

    info = db["teams"][team_key]

    if member.id == interaction.user.id:
        await interaction.followup.send(
            "You can't kick yourself. Use `/changeteamsettings delete:True` if you want that.",
            ephemeral=True,
        )
        return

    if member.id not in info.get("members", []):
        await interaction.followup.send(f"{member.mention} isn't a member of **{team_key}**.", ephemeral=True)
        return

    role = interaction.guild.get_role(info["role_id"])
    if role:
        await member.remove_roles(role, reason=f"Kicked from team by {interaction.user}")

    info["members"] = [uid for uid in info["members"] if uid != member.id]
    clear_team_join(info, member.id)
    save_db(db)
    await backup_db_to_log_channel()

    await interaction.followup.send(f"Removed {member.mention} from **{team_key}**.", ephemeral=True)


async def team_name_autocomplete(interaction: discord.Interaction, current: str):
    db = load_db()
    return [
        app_commands.Choice(name=key, value=key)
        for key in db["teams"].keys()
        if current.lower() in key.lower()
    ][:25]


@bot.tree.command(
    name="staffchangesetting",
    description="(Staff) Change a team's name, colour, or icon, or delete it",
)
@app_commands.describe(
    team="Team to modify",
    delete="Delete the team — removes the role, channel, and database entry (can't be undone)",
    changename="New team name",
    changecolour="New hex colour for the team's role, e.g. #5865F2",
    changeicon="New single standard emoji for the team (no custom server emojis)",
)
async def staffchangesetting(
    interaction: discord.Interaction,
    team: str,
    delete: bool = False,
    changename: str = None,
    changecolour: str = None,
    changeicon: str = None,
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

    if not any([changename, changecolour, changeicon]):
        await interaction.followup.send(
            "You didn't specify anything to change. Provide `changename`, `changecolour`, "
            "`changeicon`, or set `delete:` to True.",
            ephemeral=True,
        )
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

    if changename and new_name.lower() != team_key.lower():
        db["teams"][new_name] = info
        del db["teams"][team_key]
        team_key = new_name
    if changeicon:
        db["teams"][team_key]["emoji"] = new_emoji

    save_db(db)
    await backup_db_to_log_channel()

    changes = []
    if changename:
        changes.append(f"name → **{new_name}**")
    if changecolour:
        changes.append(f"colour → `{normalized_colour}`")
    if changeicon:
        changes.append(f"icon → {new_emoji}")

    message = f"✅ Updated **{team_key}**: " + ", ".join(changes)
    if icon_warning:
        message += f"\n⚠️ Everything else applied, but {icon_warning}."
    await interaction.followup.send(message, ephemeral=True)


staffchangesetting.autocomplete("team")(team_name_autocomplete)


@bot.tree.command(
    name="statssettings",
    description="(Staff) Set a team's stats shown on the team-stats panel",
)
@app_commands.describe(
    team="Team to update",
    overall="Overall rating",
    matches="Matches ever played",
    tournaments="Tournaments won",
    placement="Best ever placement",
    winrate="Win rate",
    avgkills="Average kills",
    avgdeaths="Average deaths",
    kd="Kill to death ratio",
)
async def statssettings(
    interaction: discord.Interaction,
    team: str,
    overall: str = None,
    matches: str = None,
    tournaments: str = None,
    placement: str = None,
    winrate: str = None,
    avgkills: str = None,
    avgdeaths: str = None,
    kd: str = None,
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

    provided = {
        "rating": overall,
        "matches": matches,
        "tournaments": tournaments,
        "placement": placement,
        "winrate": winrate,
        "avgkills": avgkills,
        "avgdeaths": avgdeaths,
        "kd": kd,
    }
    updates = {key: value for key, value in provided.items() if value is not None}

    if not updates:
        await interaction.followup.send(
            "You didn't provide any stats to update. Every field is optional, but you need to "
            "set at least one.",
            ephemeral=True,
        )
        return

    stats_db = load_team_stats()
    team_stats = stats_db["teams"].setdefault(team_key, {})
    team_stats.update(updates)
    save_team_stats(stats_db)
    await backup_team_stats_to_log_channel()

    changes = [f"{TEAM_STAT_FIELDS[key]} → `{value}`" for key, value in updates.items()]
    await interaction.followup.send(
        f"✅ Updated stats for **{team_key}**:\n" + "\n".join(changes), ephemeral=True
    )


statssettings.autocomplete("team")(team_name_autocomplete)


@bot.tree.command(
    name="cleanuporphanteams",
    description="(Staff) Delete channels/roles in the team category that have no matching database entry",
)
async def cleanuporphanteams(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    if not has_staff_role(interaction.user):
        await interaction.followup.send("You don't have permission to use this command.", ephemeral=True)
        return

    category = interaction.guild.get_channel(TEAM_CATEGORY_ID)
    if category is None or not isinstance(category, discord.CategoryChannel):
        await interaction.followup.send("Couldn't find the team category.", ephemeral=True)
        return

    db = load_db()
    known_channel_ids = {info["channel_id"] for info in db["teams"].values()}

    orphans = []  # list of (channel, role_or_None)
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
            "No orphaned team channels found — everything in the category matches the database.",
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
        f"Found **{len(orphans)}** channel(s) in the team category with no matching database "
        f"entry:\n" + "\n".join(lines) + "\n\nDelete them (and their linked roles)? This can't be undone.",
        view=view,
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
    team_key = find_team_by_leader(db["teams"], interaction.user.id)
    if not team_key:
        await interaction.followup.send("You must be a team leader to use this command.", ephemeral=True)
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
    description="Change your team's name, colour, or icon, or delete it (leader only)",
)
@app_commands.describe(
    delete="Delete your team — removes the role, channel, and database entry (can't be undone)",
    changename="New team name",
    changecolour="New hex colour for the team's role, e.g. #5865F2",
    changeicon="New single standard emoji for the team (no custom server emojis)",
)
async def changeteamsettings(
    interaction: discord.Interaction,
    delete: bool = False,
    changename: str = None,
    changecolour: str = None,
    changeicon: str = None,
):
    await interaction.response.defer(ephemeral=True)

    db = load_db()
    team_key = find_team_by_leader(db["teams"], interaction.user.id)
    if not team_key:
        await interaction.followup.send("You must be a team leader to use this command.", ephemeral=True)
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

    if not any([changename, changecolour, changeicon]):
        await interaction.followup.send(
            "You didn't specify anything to change. Provide `changename`, `changecolour`, "
            "`changeicon`, or set `delete:` to True.",
            ephemeral=True,
        )
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

    if changename and new_name.lower() != team_key.lower():
        db["teams"][new_name] = info
        del db["teams"][team_key]
        team_key = new_name
    if changeicon:
        db["teams"][team_key]["emoji"] = new_emoji

    save_db(db)
    await backup_db_to_log_channel()

    changes = []
    if changename:
        changes.append(f"name → **{new_name}**")
    if changecolour:
        changes.append(f"colour → `{normalized_colour}`")
    if changeicon:
        changes.append(f"icon → {new_emoji}")

    message = f"✅ Updated **{team_key}**: " + ", ".join(changes)
    if icon_warning:
        message += f"\n⚠️ Everything else applied, but {icon_warning}."
    await interaction.followup.send(message, ephemeral=True)


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

    embed = build_giveaway_embed(
        prize=prize, winners_count=winners, host_id=host.id, end_ts=end_ts, entries_count=0,
    )
    view = GiveawayJoinView()

    if os.path.exists(SUPPORT_BANNER_PATH):
        file = discord.File(SUPPORT_BANNER_PATH, filename=SUPPORT_BANNER_FILENAME)
        sent = await interaction.channel.send(embed=embed, view=view, file=file)
    else:
        embed.set_image(url=None)
        sent = await interaction.channel.send(embed=embed, view=view)

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


# ---------- Message stats slash command ----------
@bot.tree.command(name="messagestats", description="Show weekly and overall message leaderboards")
async def messagestats(interaction: discord.Interaction):
    await interaction.response.defer()

    data = load_message_stats()
    users = data.get("users", {})
    week_start = _current_week_start()

    weekly_rows = [
        (uid, entry.get("weekly", 0))
        for uid, entry in users.items()
        if entry.get("week_start") == week_start and entry.get("weekly", 0) > 0
    ]
    weekly_rows.sort(key=lambda r: r[1], reverse=True)

    overall_rows = [(uid, entry.get("overall", 0)) for uid, entry in users.items() if entry.get("overall", 0) > 0]
    overall_rows.sort(key=lambda r: r[1], reverse=True)

    def _format_rows(rows: list) -> str:
        if not rows:
            return "No messages tracked yet."
        return "\n".join(f"**{i}.** <@{uid}> — {count}" for i, (uid, count) in enumerate(rows[:10], start=1))

    embed = discord.Embed(
        title="📊 Message Stats",
        description="Tracking messages sent anywhere in the server.",
        colour=discord.Colour.blurple(),
    )
    embed.add_field(name=f"This Week (since {week_start})", value=_format_rows(weekly_rows), inline=False)
    embed.add_field(name="All-Time", value=_format_rows(overall_rows), inline=False)

    last_updated = data.get("last_updated")
    if last_updated:
        embed.set_footer(text=f"Last updated: {last_updated}")

    await interaction.followup.send(embed=embed)


@bot.event
async def on_ready():
    await restore_db_from_log_channel()
    await restore_message_stats_from_log_channel()
    await restore_team_stats_from_log_channel()
    await restore_ticket_db_from_log_channel()
    bot.add_view(SupportPanelView())
    bot.add_view(TicketCloseView())
    bot.add_view(TournamentTeamSelectView(keep_nav_buttons=True))
    bot.add_view(TournamentSubmissionView())
    bot.add_view(TeamStatsSelectView(keep_nav_buttons=True))
    bot.add_view(GiveawayJoinView())
    await bot.tree.sync()
    try:
        await sync_existing_teams()
    except discord.HTTPException as e:
        print(f"Failed to sync existing teams (leader role/permissions): {e}")
    try:
        await refresh_support_ticket_panel()
    except discord.HTTPException as e:
        print(f"Failed to refresh support ticket panel: {e}")
    try:
        await refresh_tournament_panel()
    except discord.HTTPException as e:
        print(f"Failed to refresh tournament panel: {e}")
    try:
        await refresh_team_stats_panel()
    except discord.HTTPException as e:
        print(f"Failed to refresh team stats panel: {e}")
    if not check_giveaways.is_running():
        check_giveaways.start()
    if not backup_message_stats.is_running():
        backup_message_stats.start()
    print(f"Logged in as {bot.user} (id: {bot.user.id})")
    print("Slash commands synced.")


if __name__ == "__main__":
    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        raise RuntimeError("DISCORD_TOKEN environment variable is not set")
    bot.run(token)
