import os
import io
import json
import discord
from discord import app_commands
from discord.ext import commands

# ---------- Config ----------
CONFIRM_CHANNEL_ID = 1528146431138074624   # admins confirm new teams here
TEAM_CATEGORY_ID = 1528146975554404552     # category new team channels are created in
LOG_CHANNEL_ID = 1528147225799037008       # JSON "database" backups go here

DB_FILE = "teams.json"

# ---------- Bot setup ----------
intents = discord.Intents.default()
intents.members = True  # needed to reliably resolve members / add roles

bot = commands.Bot(command_prefix="!", intents=intents)


# ---------- JSON "database" helpers ----------
def load_db() -> dict:
    if os.path.exists(DB_FILE):
        with open(DB_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_db(data: dict) -> None:
    with open(DB_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


async def backup_db_to_log_channel():
    channel = bot.get_channel(LOG_CHANNEL_ID) or await bot.fetch_channel(LOG_CHANNEL_ID)
    with open(DB_FILE, "rb") as f:
        file_bytes = f.read()
    await channel.send(
        content="📦 Teams database updated:",
        file=discord.File(io.BytesIO(file_bytes), filename=DB_FILE),
    )


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
        db = load_db()
        info = db.pop(self.team_name, None)
        if info is None:
            await interaction.response.edit_message(content="That team no longer exists.", view=None)
            return

        role = self.guild.get_role(info["role_id"])
        if role:
            await role.delete(reason=f"Team deleted by {interaction.user}")

        channel = self.guild.get_channel(info["channel_id"])
        if channel:
            await channel.delete(reason=f"Team deleted by {interaction.user}")

        save_db(db)
        await backup_db_to_log_channel()

        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(
            content=f"🗑️ Team **{self.team_name}** deleted. You can now create a new one.",
            view=self,
        )


# ---------- Admin confirmation view for /createteam ----------
class ConfirmTeamView(discord.ui.View):
    def __init__(self, requester_id: int, team_name: str, emoji: str, guild: discord.Guild):
        super().__init__(timeout=300)
        self.requester_id = requester_id
        self.team_name = team_name
        self.emoji = emoji
        self.guild = guild

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

    @discord.ui.button(label="Yes", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        await self.disable_all(interaction)

        guild = self.guild
        category = guild.get_channel(TEAM_CATEGORY_ID)

        role = await guild.create_role(
            name=f"{self.team_name} Team",
            reason=f"Team created, confirmed by {interaction.user}",
        )

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
        db[self.team_name] = {
            "emoji": self.emoji,
            "leader_id": self.requester_id,
            "role_id": role.id,
            "channel_id": team_channel.id,
            "members": [self.requester_id],
        }
        save_db(db)
        await backup_db_to_log_channel()

        await interaction.followup.send(
            f"✅ Team **{self.team_name}** {self.emoji} created — {team_channel.mention}"
        )

    @discord.ui.button(label="No", style=discord.ButtonStyle.danger)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.disable_all(interaction)
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
        db = load_db()
        info = db.get(self.team_name)
        if info is None:
            for child in self.children:
                child.disabled = True
            await interaction.response.edit_message(content="This team no longer exists.", view=self)
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
        await interaction.response.edit_message(content=f"You joined **{self.team_name}**! 🎉", view=self)

    @discord.ui.button(label="No", style=discord.ButtonStyle.danger)
    async def decline(self, interaction: discord.Interaction, button: discord.ui.Button):
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(content="Invite declined.", view=self)


# ---------- Slash commands ----------
@bot.tree.command(name="createteam", description="Create a new team")
@app_commands.describe(name="Team name", emoji="Emoji for the team")
async def createteam(interaction: discord.Interaction, name: str, emoji: str):
    db = load_db()
    existing = find_team_by_leader(db, interaction.user.id)
    if existing:
        view = DeleteTeamView(interaction.user.id, existing, interaction.guild)
        await interaction.response.send_message(
            f"You already lead a team called **{existing}**. You can only lead one team at a time.",
            view=view,
            ephemeral=True,
        )
        return

    confirm_channel = bot.get_channel(CONFIRM_CHANNEL_ID) or await bot.fetch_channel(CONFIRM_CHANNEL_ID)
    view = ConfirmTeamView(
        requester_id=interaction.user.id,
        team_name=name,
        emoji=emoji,
        guild=interaction.guild,
    )
    await confirm_channel.send(
        content=f"{interaction.user.mention} wants to create team **{name}** {emoji}. Admins, confirm?",
        view=view,
    )
    await interaction.response.send_message(
        f"Sent to {confirm_channel.mention} for admin confirmation ✅", ephemeral=True
    )


@bot.tree.command(name="teammembers", description="List a team's members")
@app_commands.describe(team="Team name")
async def teammembers(interaction: discord.Interaction, team: str):
    db = load_db()
    key = find_team_key_ci(db, team)
    if not key:
        await interaction.response.send_message("No team found with that name.", ephemeral=True)
        return

    info = db[key]
    lines = [
        f"<@{uid}>" + (" (Leader)" if uid == info["leader_id"] else "")
        for uid in info.get("members", [])
    ]
    embed = discord.Embed(
        title=f"{info['emoji']} {key} Team",
        description="\n".join(lines) if lines else "No members yet.",
    )
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="invite", description="Invite a user to your team")
@app_commands.describe(user="The user to invite")
async def invite(interaction: discord.Interaction, user: discord.Member):
    db = load_db()
    team_key = find_team_by_leader(db, interaction.user.id)
    if not team_key:
        await interaction.response.send_message("You must be a team leader to invite people.", ephemeral=True)
        return

    if user.bot:
        await interaction.response.send_message("You can't invite bots.", ephemeral=True)
        return

    if find_team_by_member(db, user.id):
        await interaction.response.send_message("That user is already in a guild.", ephemeral=True)
        return

    info = db[team_key]
    view = InviteResponseView(team_key, user.id, interaction.guild.id)
    try:
        await user.send(
            f"{interaction.user.mention} invited you to join **{team_key}** {info['emoji']}! "
            f"Would you like to join?",
            view=view,
        )
    except discord.Forbidden:
        await interaction.response.send_message(
            "Couldn't DM that user (they may have DMs off).", ephemeral=True
        )
        return

    await interaction.response.send_message(f"Invite sent to {user.mention}.", ephemeral=True)


@bot.tree.command(name="leaveteam", description="Leave your current team")
async def leaveteam(interaction: discord.Interaction):
    db = load_db()
    team_key = find_team_by_member(db, interaction.user.id)
    if not team_key:
        await interaction.response.send_message("You're not in a team.", ephemeral=True)
        return

    info = db[team_key]
    if interaction.user.id == info["leader_id"]:
        await interaction.response.send_message(
            "You're the leader of this team, so you can't leave it. "
            "Run /createteam again to get the option to delete it instead.",
            ephemeral=True,
        )
        return

    role = interaction.guild.get_role(info["role_id"])
    if role:
        await interaction.user.remove_roles(role, reason="Left the team")

    info["members"] = [uid for uid in info["members"] if uid != interaction.user.id]
    save_db(db)
    await backup_db_to_log_channel()

    await interaction.response.send_message(f"You left **{team_key}**.", ephemeral=True)


@bot.event
async def on_ready():
    await bot.tree.sync()
    print(f"Logged in as {bot.user} (id: {bot.user.id})")
    print("Slash commands synced.")


if __name__ == "__main__":
    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        raise RuntimeError("DISCORD_TOKEN environment variable is not set")
    bot.run(token)
