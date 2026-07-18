import os
import io
import json
import discord
from discord import app_commands
from discord.ext import commands

# ---------- Config ----------
CONFIRM_CHANNEL_ID = 1528146431138074624   # where the Yes/No confirmation is posted
TEAM_CATEGORY_ID = 1528146975554404552     # category the new team channel is created in
LOG_CHANNEL_ID = 1528147225799037008       # where the JSON "database" is backed up

DB_FILE = "teams.json"

# ---------- Bot setup ----------
intents = discord.Intents.default()
intents.members = True  # needed to reliably add roles / resolve members

bot = commands.Bot(command_prefix="!", intents=intents)


# ---------- Simple JSON "database" ----------
def load_db() -> dict:
    if os.path.exists(DB_FILE):
        with open(DB_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_db(data: dict) -> None:
    with open(DB_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


async def backup_db_to_log_channel():
    """Uploads the current teams.json to the log channel so there's a durable copy."""
    channel = bot.get_channel(LOG_CHANNEL_ID)
    if channel is None:
        channel = await bot.fetch_channel(LOG_CHANNEL_ID)

    with open(DB_FILE, "rb") as f:
        file_bytes = f.read()

    await channel.send(
        content="📦 Teams database updated:",
        file=discord.File(io.BytesIO(file_bytes), filename=DB_FILE),
    )


# ---------- Confirmation view ----------
class ConfirmTeamView(discord.ui.View):
    def __init__(self, author_id: int, team_name: str, emoji: str, guild: discord.Guild):
        super().__init__(timeout=120)
        self.author_id = author_id
        self.team_name = team_name
        self.emoji = emoji
        self.guild = guild

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "Only the person who ran the command can confirm this.", ephemeral=True
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

        # Create the role
        role = await guild.create_role(
            name=f"{self.team_name} team",
            reason=f"Team created by {interaction.user}",
        )

        # Create the channel inside the category
        channel_name = f"{self.emoji}┃{self.team_name}-Team"
        team_channel = await guild.create_text_channel(
            name=channel_name,
            category=category,
            reason=f"Team created by {interaction.user}",
        )

        # Give the leader the new role
        member = guild.get_member(interaction.user.id) or await guild.fetch_member(interaction.user.id)
        await member.add_roles(role, reason="New team leader")

        # DM the leader
        try:
            await interaction.user.send(
                f"You're now the leader of **{self.team_name}** {self.emoji}!"
            )
        except discord.Forbidden:
            pass  # user has DMs off; not fatal

        # Save to the JSON "database"
        db = load_db()
        db[self.team_name] = {
            "emoji": self.emoji,
            "leader_id": interaction.user.id,
            "role_id": role.id,
            "channel_id": team_channel.id,
        }
        save_db(db)
        await backup_db_to_log_channel()

        await interaction.followup.send(
            f"✅ Team **{self.team_name}** {self.emoji} created — {team_channel.mention}"
        )

    @discord.ui.button(label="No", style=discord.ButtonStyle.danger)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.disable_all(interaction)
        await interaction.response.send_message("Team creation cancelled.", ephemeral=True)


# ---------- Slash command ----------
@bot.tree.command(name="createteam", description="Create a new team")
@app_commands.describe(name="Team name", emoji="Emoji for the team")
async def createteam(interaction: discord.Interaction, name: str, emoji: str):
    confirm_channel = bot.get_channel(CONFIRM_CHANNEL_ID)
    if confirm_channel is None:
        confirm_channel = await bot.fetch_channel(CONFIRM_CHANNEL_ID)

    view = ConfirmTeamView(
        author_id=interaction.user.id,
        team_name=name,
        emoji=emoji,
        guild=interaction.guild,
    )

    await confirm_channel.send(
        content=(
            f"{interaction.user.mention} wants to create team **{name}** {emoji}. "
            f"Confirm?"
        ),
        view=view,
    )

    await interaction.response.send_message(
        f"Confirmation sent in {confirm_channel.mention} ✅", ephemeral=True
    )


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
