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
                    last_msg_time TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                    lost_streak INT DEFAULT 0,
                    last_revive TIMESTAMP WITH TIME ZONE DEFAULT NULL
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

async def update_streak_roles(member, streak_count):
    try:
        for threshold, role_id in STREAK_ROLES.items():
            role = member.guild.get_role(role_id)

            if role:
                if streak_count >= threshold and role not in member.roles:
                    await member.add_roles(role)
                elif streak_count < threshold and role in member.roles:
                    await member.remove_roles(role)
    except discord.Forbidden:
        pass


async def remove_all_streak_roles(member):
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

    with psycopg.connect(DB_URL) as conn:
        with conn.cursor() as cursor:
            cursor.execute("""
                SELECT user_id, current_streak
                FROM user_streaks
                WHERE current_streak > 0
                AND last_msg_time < %s;
            """, (now - timedelta(hours=24),))

            expired_users = cursor.fetchall()

            for user_id, old_streak in expired_users:
                cursor.execute("""
                    UPDATE user_streaks
                    SET lost_streak = current_streak,
                        current_streak = 0,
                        msg_count = 0,
                        last_streak_time = NULL
                    WHERE user_id = %s;
                """, (user_id,))

                for guild in bot_instance.guilds:
                    member = guild.get_member(user_id)

                    if member:
                        await remove_all_streak_roles(member)

                        cursor.execute("""
                            SELECT announcement_channel_id
                            FROM streak_config
                            WHERE guild_id = %s;
                        """, (guild.id,))

                        config = cursor.fetchone()

                        if config and config[0]:
                            channel = guild.get_channel(config[0])

                            if channel:
                                await channel.send(
                                    f"💔 {member.mention}, you lost your streak of **{old_streak}**!"
                                )

            conn.commit()

# ============================
# READY
# ============================

@bot.event
async def on_ready():
    init_db()
    print(f"Logged in as {bot.user}")

# ============================
# MESSAGE TRACKING
# ============================

@bot.event
async def on_message(message):
    if message.author.bot or not message.guild:
        return

    user_id = message.author.id
    now = datetime.now(timezone.utc)

    with psycopg.connect(DB_URL) as conn:
        with conn.cursor() as cursor:
            cursor.execute("""
                SELECT msg_count, current_streak, last_streak_time
                FROM user_streaks
                WHERE user_id = %s;
            """, (user_id,))

            row = cursor.fetchone()

            if not row:
                cursor.execute("""
                    INSERT INTO user_streaks
                    (user_id, msg_count, current_streak, last_msg_time)
                    VALUES (%s, 1, 0, %s);
                """, (user_id, now))

            else:
                msg_count, current_streak, last_streak_time = row
                msg_count += 1

                if msg_count >= 20:
                    if last_streak_time is None or now - last_streak_time >= timedelta(hours=24):
                        current_streak += 1
                        msg_count = 0
                        last_streak_time = now

                        await update_streak_roles(message.author, current_streak)

                cursor.execute("""
                    UPDATE user_streaks
                    SET msg_count = %s,
                        current_streak = %s,
                        last_streak_time = %s,
                        last_msg_time = %s
                    WHERE user_id = %s;
                """, (
                    msg_count,
                    current_streak,
                    last_streak_time,
                    now,
                    user_id
                ))

            conn.commit()

    await bot.process_commands(message)

# ============================
# CHANNEL CONFIG
# ============================

@bot.tree.command(name="channel")
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
# MESSAGE STREAK
# ============================

@bot.tree.command(name="messagestreak")
async def messagestreak(interaction: discord.Interaction):
    with psycopg.connect(DB_URL) as conn:
        with conn.cursor() as cursor:
            cursor.execute("""
                SELECT current_streak
                FROM user_streaks
                WHERE user_id = %s;
            """, (interaction.user.id,))

            row = cursor.fetchone()

    streak = row[0] if row else 0

    await interaction.response.send_message(
        f"You have a message streak of **{streak}**",
        ephemeral=True
    )

# ============================
# REVIVE STREAK
# ============================

@bot.tree.command(name="revivestreak")
async def revivestreak(interaction: discord.Interaction):
    now = datetime.now(timezone.utc)

    with psycopg.connect(DB_URL) as conn:
        with conn.cursor() as cursor:
            cursor.execute("""
                SELECT lost_streak, last_revive
                FROM user_streaks
                WHERE user_id = %s;
            """, (interaction.user.id,))

            row = cursor.fetchone()

            if not row or row[0] == 0:
                await interaction.response.send_message(
                    "No streak available to revive.",
                    ephemeral=True
                )
                return

            lost_streak, last_revive = row

            if last_revive and now - last_revive < timedelta(days=7):
                remaining = timedelta(days=7) - (now - last_revive)

                await interaction.response.send_message(
                    f"You can revive again in {remaining.days} day(s).",
                    ephemeral=True
                )
                return

            cursor.execute("""
                UPDATE user_streaks
                SET current_streak = %s,
                    lost_streak = 0,
                    last_revive = %s
                WHERE user_id = %s;
            """, (lost_streak, now, interaction.user.id))

            conn.commit()

    await update_streak_roles(interaction.user, lost_streak)

    await interaction.response.send_message(
        f"🔥 Your streak of **{lost_streak}** has been revived!"
    )

# ============================
# DREAM TEAM
# ============================

@bot.tree.command(name="adddreamteam")
async def add_dreamteam(interaction: discord.Interaction, user: discord.Member):
    if not any(role.id == DREAM_TEAM_MANAGER_ROLE_ID for role in interaction.user.roles):
        await interaction.response.send_message("No permission.", ephemeral=True)
        return

    role = interaction.guild.get_role(DREAM_TEAM_ROLE_ID)
    await user.add_roles(role)
    await interaction.response.send_message(f"Added Dream Team to {user.mention}")

@bot.tree.command(name="removedreamteam")
async def remove_dreamteam(interaction: discord.Interaction, user: discord.Member):
    if not any(role.id == DREAM_TEAM_MANAGER_ROLE_ID for role in interaction.user.roles):
        await interaction.response.send_message("No permission.", ephemeral=True)
        return

    role = interaction.guild.get_role(DREAM_TEAM_ROLE_ID)
    await user.remove_roles(role)
    await interaction.response.send_message(f"Removed Dream Team from {user.mention}")

# ============================
# CC ROLE
# ============================

@bot.tree.command(name="addccrole")
async def addccrole(interaction: discord.Interaction, user: discord.Member):
    if not any(role.id == CC_MANAGER_ROLE_ID for role in interaction.user.roles):
        await interaction.response.send_message("No permission.", ephemeral=True)
        return

    role = interaction.guild.get_role(CC_ROLE_ID)
    await user.add_roles(role)
    await interaction.response.send_message(f"Added CC role to {user.mention}")

@bot.tree.command(name="removeccrole")
async def removeccrole(interaction: discord.Interaction, user: discord.Member):
    if not any(role.id == CC_MANAGER_ROLE_ID for role in interaction.user.roles):
        await interaction.response.send_message("No permission.", ephemeral=True)
        return

    role = interaction.guild.get_role(CC_ROLE_ID)
    await user.remove_roles(role)
    await interaction.response.send_message(f"Removed CC role from {user.mention}")

# ============================
# RUN BOT
# ============================

token = os.environ.get("DISCORD_TOKEN")

if token:
    bot.run(token)
else:
    print("Missing DISCORD_TOKEN")
