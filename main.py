import os
import io
import re
import json
import emoji as emoji_lib
import discord
from discord import app_commands
from discord.ext import commands

# ---------- Config ----------
CONFIRM_CHANNEL_ID = 1528146431138074624   # admins confirm new teams here
TEAM_CATEGORY_ID = 1528146975554404552     # category new team channels are created in
LOG_CHANNEL_ID = 1528147225799037008       # single JSON "database" message lives here
INVITE_LOG_CHANNEL_ID = 1528160701955313722  # invite-tracking messages go here
REFERENCE_ROLE_ID = 1528009686509420616    # team roles are kept positioned just above this role
STAFF_ROLE_ID = 1528009567219224616        # only holders of this role can use staff team-management commands
SUPPORT_TICKET_CHANNEL_ID = 1528355152287760405  # the support ticket panel is posted/refreshed here

DB_FILE = "teams.json"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SUPPORT_BANNER_PATH = os.path.join(BASE_DIR, "assets", "support_banner.png")
SUPPORT_BANNER_FILENAME = "support_banner.png"

# ---------- Bot setup ----------
intents = discord.Intents.default()
intents.members = True  # needed to reliably resolve members / add roles

bot = commands.Bot(command_prefix="!", intents=intents)


# ---------- JSON "database" helpers ----------
def load_db() -> dict:
    if not os.path.exists(DB_FILE):
        return {"teams": {}, "invites": {}}
    with open(DB_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    if "teams" not in data:
        # migrate old flat-format {team_name: {...}} files
        data = {"teams": data, "invites": {}}
    data.setdefault("teams", {})
    data.setdefault("invites", {})
    return data


def save_db(data: dict) -> None:
    with open(DB_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


# Cache of the single database message so we edit it in place instead of
# posting a new file every time. Populated lazily by scanning channel history.
_db_message_cache = None

# Per-guild cache of invite code -> uses, used to detect which invite a new
# member used when they join.
invite_cache: dict = {}

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


SUPPORT_PANEL_TITLE = "Discord Support System"


def build_support_ticket_embed() -> discord.Embed:
    embed = discord.Embed(
        title=SUPPORT_PANEL_TITLE,
        description=(
            "Welcome! Before opening a ticket, please read the rules below "
            "carefully. Our team is here to help with server issues."
        ),
        colour=discord.Colour.orange(),
    )
    embed.add_field(
        name="📘 Ticket Rules",
        value=(
            "1️⃣ Please follow our server rules and stay respectful.\n"
            "2️⃣ Do not open a ticket to report in-game issues.\n"
            "3️⃣ Do not spam or open multiple tickets for the same issue.\n"
            "4️⃣ Do not use tickets to report bugs, use the proper bug report channel."
        ),
        inline=False,
    )
    embed.add_field(
        name="⏳ Response Time",
        value="If you don't respond within 48 hours, your ticket will be closed.",
        inline=False,
    )
    embed.add_field(
        name="🤔 Need Help With Something Else?",
        value=(
            "<#1528007337699311740>\n"
            "<#1528009356119900210>\n"
            "<#1528230357072347146>"
        ),
        inline=False,
    )
    embed.set_image(url=f"attachment://{SUPPORT_BANNER_FILENAME}")
    embed.set_footer(text="Animal Company: Arena Hub")
    return embed


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

    if not os.path.exists(SUPPORT_BANNER_PATH):
        print(f"Support banner image missing at {SUPPORT_BANNER_PATH} — panel sent without image.")
        await channel.send(embed=build_support_ticket_embed())
        return

    embed = build_support_ticket_embed()
    file = discord.File(SUPPORT_BANNER_PATH, filename=SUPPORT_BANNER_FILENAME)
    await channel.send(embed=embed, file=file)


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


# ---------- Confirmation view for /deleteteam ----------
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


# ---------- Admin confirmation view for /createteam ----------
class ConfirmTeamView(discord.ui.View):
    def __init__(self, requester_id: int, team_name: str, emoji: str, colour: str, guild: discord.Guild):
        super().__init__(timeout=300)
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

    async def disable_all(self, interaction: discord.Interaction):
        for child in self.children:
            child.disabled = True
        await interaction.message.edit(view=self)

    async def on_timeout(self):
        pending_team_requests.discard(self.requester_id)
        if self.message is not None:
            for child in self.children:
                child.disabled = True
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass

    @discord.ui.button(label="Yes", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        await self.disable_all(interaction)

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

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            role: discord.PermissionOverwrite(view_channel=True, send_messages=True),
            guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True),
        }

        channel_name = f"{self.emoji}┃{self.team_name}-Team"
        team_channel = await guild.create_text_channel(
            name=channel_name,
            category=category,
            overwrites=overwrites,
            reason=f"Team created, confirmed by {interaction.user}",
        )

        leader = guild.get_member(self.requester_id) or await guild.fetch_member(self.requester_id)
        await leader.add_roles(role, reason="New team leader")

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
        await self.disable_all(interaction)
        pending_team_requests.discard(self.requester_id)
        await interaction.response.send_message("Team creation denied.", ephemeral=True)


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
            "Ask a staff member to delete the team with /deleteteam if that's what you want.",
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
            "You can't kick yourself. Ask a staff member to use /deleteteam if you want that.",
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


@bot.tree.command(name="deleteteam", description="(Staff) Delete a team's role, channel, and database entry")
@app_commands.describe(team="Team to delete")
async def deleteteam(interaction: discord.Interaction, team: str):
    await interaction.response.defer(ephemeral=True)

    if not has_staff_role(interaction.user):
        await interaction.followup.send("You don't have permission to use this command.", ephemeral=True)
        return

    db = load_db()
    key = find_team_key_ci(db["teams"], team)
    if not key:
        await interaction.followup.send("No team found with that name.", ephemeral=True)
        return

    view = ConfirmDeleteTeamView(interaction.user.id, key, interaction.guild)
    await interaction.followup.send(
        f"Are you sure you want to delete **{key}**? This will remove the team's role, channel, "
        f"and database entry, and can't be undone.",
        view=view,
        ephemeral=True,
    )


deleteteam.autocomplete("team")(team_name_autocomplete)


@bot.tree.command(name="recolour", description="(Staff) Change a team's role colour")
@app_commands.describe(team="Team to recolour", hex="New hex colour, e.g. #5865F2")
async def recolour(interaction: discord.Interaction, team: str, hex: str):
    await interaction.response.defer(ephemeral=True)

    if not has_staff_role(interaction.user):
        await interaction.followup.send("You don't have permission to use this command.", ephemeral=True)
        return

    normalized_colour = normalize_hex_colour(hex)
    if normalized_colour is None:
        await interaction.followup.send(
            "That's not a valid hex colour. Use a format like `#5865F2`.", ephemeral=True
        )
        return

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

    await role.edit(
        colour=discord.Colour.from_str(normalized_colour),
        reason=f"Colour changed by staff member {interaction.user}",
    )

    await interaction.followup.send(
        f"✅ **{key}**'s colour is now `{normalized_colour}`.", ephemeral=True
    )


recolour.autocomplete("team")(team_name_autocomplete)


@bot.tree.command(name="rename", description="(Staff) Rename a team")
@app_commands.describe(team="Team to rename", name="New team name")
async def rename(interaction: discord.Interaction, team: str, name: str):
    await interaction.response.defer(ephemeral=True)

    if not has_staff_role(interaction.user):
        await interaction.followup.send("You don't have permission to use this command.", ephemeral=True)
        return

    db = load_db()
    key = find_team_key_ci(db["teams"], team)
    if not key:
        await interaction.followup.send("No team found with that name.", ephemeral=True)
        return

    if name.lower() == key.lower():
        await interaction.followup.send("That's already this team's name.", ephemeral=True)
        return

    if find_team_key_ci(db["teams"], name):
        await interaction.followup.send(
            f"A team called **{name}** already exists. Pick a different name.", ephemeral=True
        )
        return

    info = db["teams"][key]
    role = interaction.guild.get_role(info["role_id"])
    channel = interaction.guild.get_channel(info["channel_id"])

    if role:
        try:
            await role.edit(name=f"{name} Team", reason=f"Team renamed by staff member {interaction.user}")
        except discord.HTTPException:
            await interaction.followup.send(
                "Couldn't rename the role — Discord rejected the new name (check length/characters).",
                ephemeral=True,
            )
            return
    if channel:
        try:
            await channel.edit(
                name=f"{info['emoji']}┃{name}-Team", reason=f"Team renamed by staff member {interaction.user}"
            )
        except discord.HTTPException:
            await interaction.followup.send(
                "Renamed the role, but Discord rejected the new channel name "
                "(check length/characters). Team is now inconsistently named — please fix manually.",
                ephemeral=True,
            )
            return

    db["teams"][name] = info
    del db["teams"][key]
    save_db(db)
    await backup_db_to_log_channel()

    await interaction.followup.send(f"✅ **{key}** has been renamed to **{name}**.", ephemeral=True)


rename.autocomplete("team")(team_name_autocomplete)


@bot.tree.command(name="changeteamcolour", description="Change your team's role colour (leader only)")
@app_commands.describe(colour="New hex colour for the team's role, e.g. #5865F2")
async def changeteamcolour(interaction: discord.Interaction, colour: str):
    await interaction.response.defer(ephemeral=True)

    normalized_colour = normalize_hex_colour(colour)
    if normalized_colour is None:
        await interaction.followup.send(
            "That's not a valid hex colour. Use a format like `#5865F2`.", ephemeral=True
        )
        return

    db = load_db()
    team_key = find_team_by_leader(db["teams"], interaction.user.id)
    if not team_key:
        await interaction.followup.send("You must be a team leader to use this command.", ephemeral=True)
        return

    info = db["teams"][team_key]
    role = interaction.guild.get_role(info["role_id"])
    if role is None:
        await interaction.followup.send("That team's role no longer exists.", ephemeral=True)
        return

    await role.edit(
        colour=discord.Colour.from_str(normalized_colour),
        reason=f"Colour changed by {interaction.user}",
    )

    await interaction.followup.send(
        f"✅ **{team_key}**'s colour is now `{normalized_colour}`.", ephemeral=True
    )


@bot.tree.command(name="changeteamname", description="Rename your team (leader only)")
@app_commands.describe(name="New team name")
async def changeteamname(interaction: discord.Interaction, name: str):
    await interaction.response.defer(ephemeral=True)

    db = load_db()
    team_key = find_team_by_leader(db["teams"], interaction.user.id)
    if not team_key:
        await interaction.followup.send("You must be a team leader to use this command.", ephemeral=True)
        return

    if name.lower() == team_key.lower():
        await interaction.followup.send("That's already this team's name.", ephemeral=True)
        return

    if find_team_key_ci(db["teams"], name):
        await interaction.followup.send(
            f"A team called **{name}** already exists. Pick a different name.", ephemeral=True
        )
        return

    info = db["teams"][team_key]
    role = interaction.guild.get_role(info["role_id"])
    channel = interaction.guild.get_channel(info["channel_id"])

    if role:
        try:
            await role.edit(name=f"{name} Team", reason=f"Team renamed by {interaction.user}")
        except discord.HTTPException:
            await interaction.followup.send(
                "Couldn't rename the role — Discord rejected the new name (check length/characters).",
                ephemeral=True,
            )
            return
    if channel:
        try:
            await channel.edit(
                name=f"{info['emoji']}┃{name}-Team", reason=f"Team renamed by {interaction.user}"
            )
        except discord.HTTPException:
            await interaction.followup.send(
                "Renamed the role, but Discord rejected the new channel name "
                "(check length/characters). Team is now inconsistently named — please fix manually.",
                ephemeral=True,
            )
            return

    db["teams"][name] = info
    del db["teams"][team_key]
    save_db(db)
    await backup_db_to_log_channel()

    await interaction.followup.send(f"✅ **{team_key}** has been renamed to **{name}**.", ephemeral=True)


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
    await bot.tree.sync()
    for guild in bot.guilds:
        try:
            invites = await guild.invites()
            invite_cache[guild.id] = {inv.code: inv.uses for inv in invites}
        except discord.Forbidden:
            invite_cache[guild.id] = {}
    try:
        await refresh_support_ticket_panel()
    except discord.HTTPException as e:
        print(f"Failed to refresh support ticket panel: {e}")
    print(f"Logged in as {bot.user} (id: {bot.user.id})")
    print("Slash commands synced.")


@bot.event
async def on_invite_create(invite: discord.Invite):
    invite_cache.setdefault(invite.guild.id, {})[invite.code] = invite.uses


@bot.event
async def on_invite_delete(invite: discord.Invite):
    invite_cache.get(invite.guild.id, {}).pop(invite.code, None)


@bot.event
async def on_member_join(member: discord.Member):
    guild = member.guild

    try:
        invites_now = await guild.invites()
    except discord.Forbidden:
        return  # bot lacks Manage Server permission; can't tell which invite was used

    before = invite_cache.get(guild.id, {})
    used_invite = None
    for inv in invites_now:
        if inv.uses is not None and inv.uses > before.get(inv.code, 0):
            used_invite = inv
            break

    invite_cache[guild.id] = {inv.code: inv.uses for inv in invites_now}

    if used_invite is None or used_invite.inviter is None:
        return  # e.g. vanity URL invite, or the diff couldn't be determined

    inviter = used_invite.inviter
    db = load_db()
    key = str(inviter.id)
    db["invites"][key] = db["invites"].get(key, 0) + 1
    count = db["invites"][key]
    save_db(db)
    await backup_db_to_log_channel()

    log_channel = bot.get_channel(INVITE_LOG_CHANNEL_ID) or await bot.fetch_channel(INVITE_LOG_CHANNEL_ID)
    await log_channel.send(
        f"{member.mention} has been invited by {inviter.mention} and has now {count} invites."
    )


if __name__ == "__main__":
    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        raise RuntimeError("DISCORD_TOKEN environment variable is not set")
    bot.run(token)
