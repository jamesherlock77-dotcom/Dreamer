import os
import io
import re
import json
import logging
import emoji as emoji_lib
import discord
from discord import app_commands
from discord.ext import commands

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
LOG_CHANNEL_ID = 1528147225799037008       # single JSON "database" message lives here
REFERENCE_ROLE_ID = 1528009686509420616    # team roles are kept positioned just above this role
STAFF_ROLE_ID = 1528009567219224616        # only holders of this role can use staff team-management commands
PREMIUM_ROLE_ID = 1528139462159106059      # gates /premiumteamsettings; premium team roles are kept above this role
CREATE_TEAM_ROLE_ID = 1528160422857932868  # required to use /createteam (pre-existing teams are grandfathered in)
TEAM_LEADER_ROLE_ID = 1528445357317423135  # granted to every team leader, current and future
MAX_TEAM_MEMBERS = 20                      # includes the leader
SUPPORT_TICKET_CHANNEL_ID = 1528355152287760405  # the support ticket panel is posted/refreshed here

DB_FILE = "teams.json"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SUPPORT_BANNER_PATH = os.path.join(BASE_DIR, "support_banner.png")
SUPPORT_BANNER_FILENAME = "support_banner.png"

# ---------- Bot setup ----------
intents = discord.Intents.default()
intents.members = True  # needed to reliably resolve members / add roles

bot = commands.Bot(command_prefix="!", intents=intents)


# ---------- JSON "database" helpers ----------
def load_db() -> dict:
    if not os.path.exists(DB_FILE):
        return {"teams": {}}
    with open(DB_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    if "teams" not in data:
        # migrate old flat-format {team_name: {...}} files
        data = {"teams": data}
    data.setdefault("teams", {})
    return data


def save_db(data: dict) -> None:
    with open(DB_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


# Cache of the single database message so we edit it in place instead of
# posting a new file every time. Populated lazily by scanning channel history.
_db_message_cache = None

# User IDs with a /createteam request currently awaiting admin confirmation,
# so the same user can't queue up multiple pending requests.
pending_team_requests: set = set()


async def get_or_create_db_message():
    global _db_message_cache
    if _db_message_cache is not None:
        return _db_message_cache

    channel = bot.get_channel(LOG_CHANNEL_ID) or await bot.fetch_channel(LOG_CHANNEL_ID)
    async for msg in channel.history(limit=50):
        if msg.author.id == bot.user.id and msg.attachments and msg.attachments[0].filename == DB_FILE:
            _db_message_cache = msg
            return msg
    return None


async def backup_db_to_log_channel():
    """Keeps a single message in the log channel updated with the current database,
    editing it in place rather than posting a new file every time."""
    global _db_message_cache

    channel = bot.get_channel(LOG_CHANNEL_ID) or await bot.fetch_channel(LOG_CHANNEL_ID)
    with open(DB_FILE, "rb") as f:
        file_bytes = f.read()
    new_file = discord.File(io.BytesIO(file_bytes), filename=DB_FILE)

    msg = await get_or_create_db_message()
    if msg is not None:
        try:
            edited = await msg.edit(content="📦 Database (auto-updated):", attachments=[new_file])
            _db_message_cache = edited
            return
        except discord.HTTPException:
            pass  # message may have been deleted; fall through and send a fresh one

    sent = await channel.send(content="📦 Database (auto-updated):", file=new_file)
    _db_message_cache = sent


async def restore_db_from_log_channel():
    """Pulls the last known database backup from the log channel into local storage.
    Critical because Railway wipes the container's disk on every redeploy — without this,
    every restart would silently start from an empty database even though a good backup
    is sitting in Discord."""
    global _db_message_cache

    if os.path.exists(DB_FILE):
        return  # local data already present (e.g. a crash-restart, not a fresh container)

    try:
        channel = bot.get_channel(LOG_CHANNEL_ID) or await bot.fetch_channel(LOG_CHANNEL_ID)
        async for msg in channel.history(limit=50):
            if msg.author.id == bot.user.id and msg.attachments and msg.attachments[0].filename == DB_FILE:
                data = await msg.attachments[0].read()
                with open(DB_FILE, "wb") as f:
                    f.write(data)
                _db_message_cache = msg
                print("Restored database from log channel backup.")
                return
        print("No existing database backup found in log channel — starting fresh.")
    except discord.HTTPException as e:
        print(f"Failed to restore database from log channel: {e}")


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


def find_team_key_ci(db: dict, name: str):
    name_lower = name.lower()
    for key in db:
        if key.lower() == name_lower:
            return key
    return None


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
    return any(role.id in (PREMIUM_ROLE_ID, STAFF_ROLE_ID) for role in member.roles)


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


# ---------- Cosmetic dropdown shown under the support ticket panel banner ----------
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
        # Cosmetic only for now — no ticket creation logic wired up yet.
        await interaction.response.send_message(
            "Ticket creation isn't set up yet — check back soon!", ephemeral=True
        )


async def refresh_support_ticket_panel():
    """Deletes any previously posted support ticket panel in the target channel and
    posts a fresh one. Called on every bot startup so the panel never goes stale or
    duplicates across restarts."""
    channel = bot.get_channel(SUPPORT_TICKET_CHANNEL_ID) or await bot.fetch_channel(SUPPORT_TICKET_CHANNEL_ID)

    async for msg in channel.history(limit=50):
        if msg.author.id == bot.user.id and msg.embeds and msg.embeds[0].title == SUPPORT_PANEL_TITLE:
            try:
                await msg.delete()
            except discord.HTTPException:
                pass

    view = SupportPanelView()

    if not os.path.exists(SUPPORT_BANNER_PATH):
        print(f"Support banner image missing at {SUPPORT_BANNER_PATH} — panel sent without image.")
        await channel.send(embed=build_support_ticket_embed(), view=view)
        return

    embed = build_support_ticket_embed()
    file = discord.File(SUPPORT_BANNER_PATH, filename=SUPPORT_BANNER_FILENAME)
    await channel.send(embed=embed, file=file, view=view)


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
            "You don't have permission to create a team.", ephemeral=True
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

    role = interaction.guild.get_role(info["role_id"])
    if role:
        await interaction.user.remove_roles(role, reason="Left the team")

    info["members"] = [uid for uid in info["members"] if uid != interaction.user.id]
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
                            reason=f"Team settings changed by {interaction.user}", **role_edit_kwargs
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


@bot.tree.command(
    name="registerteam",
    description="(Admin) Re-link an existing role/channel/leader into the database",
)
@app_commands.default_permissions(administrator=True)
@app_commands.describe(
    name="Team name",
    role="The team's existing role",
    channel="The team's existing channel",
    leader="The team leader",
    emoji="Optional: override the emoji (auto-detected from the channel name if omitted)",
    colour="Optional: override the hex colour (auto-detected from the role's colour if omitted)",
    member1="Optional additional member",
    member2="Optional additional member",
    member3="Optional additional member",
    member4="Optional additional member",
    member5="Optional additional member",
)
async def registerteam(
    interaction: discord.Interaction,
    name: str,
    role: discord.Role,
    channel: discord.TextChannel,
    leader: discord.Member,
    emoji: str = None,
    colour: str = None,
    member1: discord.Member = None,
    member2: discord.Member = None,
    member3: discord.Member = None,
    member4: discord.Member = None,
    member5: discord.Member = None,
):
    await interaction.response.defer(ephemeral=True)

    if not interaction.user.guild_permissions.administrator:
        await interaction.followup.send("Only admins can use this command.", ephemeral=True)
        return

    if emoji is None:
        candidate = channel.name.split("┃", 1)[0] if "┃" in channel.name else None
        if candidate and is_valid_standard_emoji(candidate):
            emoji = candidate
        else:
            await interaction.followup.send(
                "Couldn't detect an emoji from that channel's name — pass `emoji:` manually.",
                ephemeral=True,
            )
            return
    elif not is_valid_standard_emoji(emoji):
        await interaction.followup.send(
            "That's not a standard Discord emoji. Please use a single regular emoji.", ephemeral=True
        )
        return

    if colour is None:
        normalized_colour = f"#{role.colour.value:06x}"
    else:
        normalized_colour = normalize_hex_colour(colour)
        if normalized_colour is None:
            await interaction.followup.send(
                "That's not a valid hex colour. Use a format like `#5865F2`.", ephemeral=True
            )
            return

    db = load_db()
    if find_team_key_ci(db["teams"], name):
        await interaction.followup.send(
            f"A team called **{name}** already exists in the database. "
            f"Pick a different name or check /teammembers first.",
            ephemeral=True,
        )
        return

    members = [leader.id]
    for extra in (member1, member2, member3, member4, member5):
        if extra is not None and extra.id not in members:
            members.append(extra.id)

    leader_marker_role = interaction.guild.get_role(TEAM_LEADER_ROLE_ID)
    if leader_marker_role is not None:
        try:
            await leader.add_roles(leader_marker_role, reason="Registered as existing team leader")
        except discord.HTTPException:
            pass

    try:
        await channel.set_permissions(
            leader,
            overwrite=team_leader_channel_overwrite(),
            reason="Registered as existing team leader",
        )
    except discord.HTTPException:
        pass

    db["teams"][name] = {
        "emoji": emoji,
        "leader_id": leader.id,
        "role_id": role.id,
        "channel_id": channel.id,
        "members": members,
    }
    save_db(db)
    await backup_db_to_log_channel()

    await interaction.followup.send(
        f"✅ Registered **{name}** {emoji} — role {role.mention}, channel {channel.mention}, "
        f"leader {leader.mention}, {len(members)} member(s) total. This is now saved and will "
        f"survive redeploys.",
        ephemeral=True,
    )


@bot.event
async def on_ready():
    await restore_db_from_log_channel()
    bot.add_view(SupportPanelView())
    await bot.tree.sync()
    try:
        await sync_existing_teams()
    except discord.HTTPException as e:
        print(f"Failed to sync existing teams (leader role/permissions): {e}")
    try:
        await refresh_support_ticket_panel()
    except discord.HTTPException as e:
        print(f"Failed to refresh support ticket panel: {e}")
    print(f"Logged in as {bot.user} (id: {bot.user.id})")
    print("Slash commands synced.")


if __name__ == "__main__":
    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        raise RuntimeError("DISCORD_TOKEN environment variable is not set")
    bot.run(token)
