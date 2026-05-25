import os
from datetime import datetime, timedelta, timezone

import psycopg
import discord
from discord.ext import commands, tasks

# ============================
# CONFIG
# ============================

DB_URL = os.environ.get("DATABASE_URL")

STREAK_ROLES = {
    1: 1495573627217641604,
    3: 1495573632984813639,
    7: 1495573635459448842,
    14: 1495573637800136844,
    30: 1495573640132034670,
    60: 1495573754921877687,
    100: 1495573763004039380
}

DREAM_TEAM_ROLE_ID = 1497337678960791632
DREAM_TEAM_MANAGER_ROLE_ID = 1508231579288342569

CC_ROLE_ID = 1495165348654219344
CC_MANAGER_ROLE_ID = 1508601647880736899

# ============================
# DATABASE INIT
# ============================

def init_db():
    with psycopg.connect(DB_URL) as conn:
        with conn.cursor() as cursor:
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS streak_config (
                    guild_id BIGINT PRIMARY KEY,
                    announcement_channel_id BIGINT
                );
            """)

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS user_streaks (
                    user_id BIGINT PRIMARY KEY,
                    msg_count INT DEFAULT 0,
                    current_streak INT DEFAULT 0,
                    last_streak_time TIMESTAMP WITH TIME ZONE DEFAULT NULL,
                    last_msg_time TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                );
            """)
            conn.commit()

# ============================
# BOT SETUP
# ============================

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

class UnifiedBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        await self.tree.sync()
        streak_expiry_check.start(self)

bot = UnifiedBot()

# ============================
# ROLE HELPERS
# ============================

async def update_streak_roles(member: discord.Member, streak_count: int):
    try:
        if streak_count in STREAK_ROLES:
            role = member.guild.get_role(STREAK_ROLES[streak_count])
            if role and role not in member.roles:
                await member.add_roles(role)
    except discord.Forbidden:
        pass

async def remove_all_streak_roles(member: discord.Member):
    try:
        roles = [member.guild.get_role(r) for r in STREAK_ROLES.values()]
        roles = [r for r in roles if r and r in member.roles]
        if roles:
            await member.remove_roles(*roles)
    except discord.Forbidden:
        pass

# ============================
# STREAK EXPIRY LOOP
# ============================

@tasks.loop(seconds=30)
async def streak_expiry_check(bot_instance):
    now = datetime.now(timezone.utc)
    expiry_limit = timedelta(hours=24)

    with psycopg.connect(DB_URL) as conn:
        with conn.cursor() as cursor:
            cursor.execute("""
                SELECT user_id, current_streak
                FROM user_streaks
                WHERE current_streak > 0
                AND last_msg_time < %s;
            """, (now - expiry_limit,))

            expired_users = cursor.fetchall()

            for user_id, old_streak in expired_users:
                cursor.execute("""
                    UPDATE user_streaks
                    SET current_streak = 0,
                        msg_count = 0,
                        last_streak_time = NULL
                    WHERE user_id = %s;
                """, (user_id,))

                for guild in bot_instance.guilds:
                    member = guild.get_member(user_id)
                    if member:
                        await remove_all_streak_roles(member)

                        cursor.execute(
                            "SELECT announcement_channel_id FROM streak_config WHERE guild_id = %s;",
                            (guild.id,)
                        )
                        config = cursor.fetchone()

                        if config and config[0]:
                            channel = guild.get_channel(config[0])
                            if channel:
                                await channel.send(
                                    f"💔 {member.mention}, you have lost your streak!"
                                )

            conn.commit()

# ============================
# ON READY
# ============================

@bot.event
async def on_ready():
    init_db()
    print(f"Logged in as {bot.user.name}")

# ============================
# /channel COMMAND
# ============================

@bot.tree.command(name="channel", description="Sets the channel for streak alerts.")
async def channel_config(interaction: discord.Interaction, channel: discord.TextChannel):
    if not interaction.user.guild_permissions.manage_channels:
        await interaction.response.send_message("Missing permissions.", ephemeral=True)
        return

    with psycopg.connect(DB_URL) as conn:
        with conn.cursor() as cursor:
            cursor.execute("""
                INSERT INTO streak_config (guild_id, announcement_channel_id)
                VALUES (%s, %s)
                ON CONFLICT (guild_id)
                DO UPDATE SET announcement_channel_id = EXCLUDED.announcement_channel_id;
            """, (interaction.guild_id, channel.id))
            conn.commit()

    await interaction.response.send_message(
        f"Streak announcements set to {channel.mention}.",
        ephemeral=True
    )

# ============================
# /messagestreak COMMAND
# ============================

@bot.tree.command(name="messagestreak", description="View your current message streak.")
async def messagestreak(interaction: discord.Interaction):
    user_id = interaction.user.id

    with psycopg.connect(DB_URL) as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT current_streak FROM user_streaks WHERE user_id = %s;", (user_id,))
            row = cursor.fetchone()

            if not row:
                await interaction.response.send_message(
                    "You have a message streak of `0`",
                    ephemeral=True
                )
                return

            streak = row[0]

    await interaction.response.send_message(
        f"You have a message streak of `{streak}`",
        ephemeral=True
    )

# ============================
# DREAM TEAM COMMANDS
# ============================

@bot.tree.command(name="adddreamteam", description="Add a user to Dream Team.")
async def add_dreamteam(interaction: discord.Interaction, user: discord.Member):

    if not any(role.id == DREAM_TEAM_MANAGER_ROLE_ID for role in interaction.user.roles):
        await interaction.response.send_message(
            "You do not have permission to use this command.",
            ephemeral=True
        )
        return

    role = interaction.guild.get_role(DREAM_TEAM_ROLE_ID)

    if not role:
        await interaction.response.send_message(
            "Dream Team role not found.",
            ephemeral=True
        )
        return

    if role in user.roles:
        await interaction.response.send_message(
            f"{user.mention} already has the Dream Team role.",
            ephemeral=True
        )
        return

    try:
        await user.add_roles(role)
        await interaction.response.send_message(
            f"Added Dream Team role to {user.mention}."
        )
    except discord.Forbidden:
        await interaction.response.send_message(
            "I don't have permission to add that role.",
            ephemeral=True
        )

@bot.tree.command(name="removedreamteam", description="Remove a user from Dream Team.")
async def remove_dreamteam(interaction: discord.Interaction, user: discord.Member):

    if not any(role.id == DREAM_TEAM_MANAGER_ROLE_ID for role in interaction.user.roles):
        await interaction.response.send_message(
            "You do not have permission to use this command.",
            ephemeral=True
        )
        return

    role = interaction.guild.get_role(DREAM_TEAM_ROLE_ID)

    if not role:
        await interaction.response.send_message(
            "Dream Team role not found.",
            ephemeral=True
        )
        return

    if role not in user.roles:
        await interaction.response.send_message(
            f"{user.mention} does not have the Dream Team role.",
            ephemeral=True
        )
        return

    try:
        await user.remove_roles(role)
        await interaction.response.send_message(
            f"Removed Dream Team role from {user.mention}."
        )
    except discord.Forbidden:
        await interaction.response.send_message(
            "I don't have permission to remove that role.",
            ephemeral=True
        )

# ============================
# CC ROLE COMMANDS
# ============================

@bot.tree.command(name="addccrole", description="Add CC role to a user.")
async def addccrole(interaction: discord.Interaction, user: discord.Member):

    if not any(role.id == CC_MANAGER_ROLE_ID for role in interaction.user.roles):
        await interaction.response.send_message(
            "You do not have permission to use this command.",
            ephemeral=True
        )
        return

    role = interaction.guild.get_role(CC_ROLE_ID)

    if not role:
        await interaction.response.send_message(
            "CC role not found.",
            ephemeral=True
        )
        return

    if role in user.roles:
        await interaction.response.send_message(
            f"{user.mention} already has the CC role.",
            ephemeral=True
        )
        return

    try:
        await user.add_roles(role)
        await interaction.response.send_message(
            f"Added CC role to {user.mention}."
        )
    except discord.Forbidden:
        await interaction.response.send_message(
            "I don't have permission to add that role.",
            ephemeral=True
        )

@bot.tree.command(name="removeccrole", description="Remove CC role from a user.")
async def removeccrole(interaction: discord.Interaction, user: discord.Member):

    if not any(role.id == CC_MANAGER_ROLE_ID for role in interaction.user.roles):
        await interaction.response.send_message(
            "You do not have permission to use this command.",
            ephemeral=True
        )
        return

    role = interaction.guild.get_role(CC_ROLE_ID)

    if not role:
        await interaction.response.send_message(
            "CC role not found.",
            ephemeral=True
        )
        return

    if role not in user.roles:
        await interaction.response.send_message(
            f"{user.mention} does not have the CC role.",
            ephemeral=True
        )
        return

    try:
        await user.remove_roles(role)
        await interaction.response.send_message(
            f"Removed CC role from {user.mention}."
        )
    except discord.Forbidden:
        await interaction.response.send_message(
            "I don't have permission to remove that role.",
            ephemeral=True
        )

# ============================
# MESSAGE HANDLER
# ============================

# Keep your original on_message exactly as before

# ============================
# RUN BOT
# ============================

token = os.environ.get("DISCORD_TOKEN")
if token:
    bot.run(token)
else:
    print("Missing DISCORD_TOKEN")
