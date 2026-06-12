import discord
from discord.ext import commands, tasks
from discord import app_commands
import os
from datetime import datetime, time
import pytz

# ── Config ───────────────────────────────────────────────────────────────────
TOKEN          = os.environ["DISCORD_BOT_TOKEN"]
DB_CHANNEL_ID  = 1515064641246466113   # your "database" channel
RESET_TIME     = time(23, 0)           # 23:00
RESET_WEEKDAY  = 6                     # Sunday (Monday=0 … Sunday=6)
TIMEZONE       = pytz.timezone("UTC") # e.g. "Europe/London"

# ── Bot setup ─────────────────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

# ── Log every message into the DB channel ────────────────────────────────────
@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return
    # Don't log messages sent inside the DB channel or the excluded channel
    if message.channel.id in (DB_CHANNEL_ID, 1500327292830875898):
        return

    db_channel = bot.get_channel(DB_CHANNEL_ID)
    if db_channel:
        await db_channel.send(
            f"{message.author.id}"
        )

    await bot.process_commands(message)

# ── Helper: read DB channel and tally counts ──────────────────────────────────
async def tally_counts() -> dict:
    """Read all messages in the DB channel and return {user_id: count}."""
    db_channel = bot.get_channel(DB_CHANNEL_ID)
    counts = {}
    async for msg in db_channel.history(limit=None, oldest_first=True):
        if msg.author.bot:
            uid = msg.content.strip()
            if uid.isdigit():
                counts[uid] = counts.get(uid, 0) + 1
    return counts

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

# ── Weekly reset ──────────────────────────────────────────────────────────────
@tasks.loop(time=RESET_TIME)
async def weekly_reset():
    now = datetime.now(TIMEZONE)
    if now.weekday() != RESET_WEEKDAY:
        return

    db_channel = bot.get_channel(DB_CHANNEL_ID)
    if not db_channel:
        print("Could not find DB channel — reset aborted.")
        return

    # Purge all messages in the DB channel
    deleted = await db_channel.purge(limit=None)
    print(f"[{now}] Weekly reset — deleted {len(deleted)} log entries.")

# ── Startup ───────────────────────────────────────────────────────────────────
@bot.event
async def on_ready():
    await tree.sync()
    weekly_reset.start()
    print(f"Logged in as {bot.user} ({bot.user.id})")

bot.run(TOKEN)
