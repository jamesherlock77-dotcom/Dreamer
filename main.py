import discord
from discord.ext import commands, tasks
from discord import app_commands
import json
import os
from datetime import datetime, time
import pytz

# ── Config ──────────────────────────────────────────────────────────────────
TOKEN = os.environ["DISCORD_BOT_TOKEN"]
COUNTS_FILE = "message_counts.json"
RESET_TIME = time(23, 0)          # 23:00
RESET_WEEKDAY = 6                  # Sunday (Monday=0 … Sunday=6)
TIMEZONE = pytz.timezone("UTC")   # Change to your timezone e.g. "Europe/London"

# ── Persistence helpers ──────────────────────────────────────────────────────
def load_counts() -> dict:
    if os.path.exists(COUNTS_FILE):
        with open(COUNTS_FILE, "r") as f:
            return json.load(f)
    return {}

def save_counts(data: dict):
    with open(COUNTS_FILE, "w") as f:
        json.dump(data, f, indent=2)

message_counts: dict = load_counts()   # { "user_id_str": count }

# ── Bot setup ────────────────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

# ── Count every message ──────────────────────────────────────────────────────
@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return
    uid = str(message.author.id)
    message_counts[uid] = message_counts.get(uid, 0) + 1
    save_counts(message_counts)
    await bot.process_commands(message)

# ── /messageleaderboard ──────────────────────────────────────────────────────
@tree.command(name="messageleaderboard", description="Show the weekly message leaderboard")
async def messageleaderboard(interaction: discord.Interaction):
    if not message_counts:
        await interaction.response.send_message("No messages tracked yet!", ephemeral=True)
        return

    sorted_users = sorted(message_counts.items(), key=lambda x: x[1], reverse=True)[:10]

    lines = []
    for i, (uid, count) in enumerate(sorted_users):
        member = interaction.guild.get_member(int(uid))
        name = member.display_name if member else f"<@{uid}>"
        lines.append(f"`{i + 1}` {name} — **{count}** msgs")

    embed = discord.Embed(
        title="Weekly Message Leaderboard",
        description=(
            "The top 10 most active members this week:\n\n"
            + "\n".join(lines)
        ),
        color=0x808080,   # grey
    )
    embed.set_footer(text="Resets every Sunday at 23:00 UTC")
    embed.timestamp = discord.utils.utcnow()

    await interaction.response.send_message(embed=embed)

# ── Weekly reset task ────────────────────────────────────────────────────────
@tasks.loop(time=RESET_TIME)
async def weekly_reset():
    now = datetime.now(TIMEZONE)
    if now.weekday() == RESET_WEEKDAY:
        message_counts.clear()
        save_counts(message_counts)
        print(f"[{now}] Weekly leaderboard reset.")

# ── Startup ──────────────────────────────────────────────────────────────────
@bot.event
async def on_ready():
    await tree.sync()
    weekly_reset.start()
    print(f"Logged in as {bot.user} ({bot.user.id})")

bot.run(TOKEN)
