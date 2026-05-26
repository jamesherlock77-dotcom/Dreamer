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
# DATABASE
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

            cursor.execute("""
                ALTER TABLE user_streaks
                ADD COLUMN IF NOT EXISTS lost_streak INT DEFAULT 0;
            """)

            cursor.execute("""
                ALTER TABLE user_streaks
                ADD COLUMN IF NOT EXISTS last_revive TIMESTAMP WITH TIME ZONE DEFAULT NULL;
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
        init_db()
        streak_expiry_check.start()
        await self.tree.sync()

bot = UnifiedBot()

# ============================
# ROLE HELPERS
# ============================

async def update_streak_roles(member, streak):
    try:
        for threshold, role_id in STREAK_ROLES.items():
            role = member.guild.get_role(role_id)

            if role:
                if streak >= threshold and role not in member.roles:
                    await member.add_roles(role)
                elif streak < threshold and role in member.roles:
                    await member.remove_roles(role)
    except discord.Forbidden:
        pass

async def remove_all_streak_roles(member):
    try:
        roles = []

        for role_id in STREAK_ROLES.values():
            role = member.guild.get_role(role_id)
            if role and role in member.roles:
                roles.append(role)

        if roles:
            await member.remove_roles(*roles)

    except discord.Forbidden:
        pass

# ============================
# STREAK EXPIRY
# ============================

@tasks.loop(seconds=30)
async def streak_expiry_check():
    now = datetime.now(timezone.utc)

    with psycopg.connect(DB_URL) as conn:
        with conn.cursor() as cursor:

            cursor.execute("""
                SELECT user_id, current_streak
                FROM user_streaks
                WHERE current_streak > 0
                AND last_msg_time < %s
            """, (now - timedelta(hours=24),))

            expired = cursor.fetchall()

            for user_id, old_streak in expired:
                cursor.execute("""
                    UPDATE user_streaks
                    SET lost_streak = current_streak,
                        current_streak = 0,
                        msg_count = 0,
                        last_streak_time = NULL
                    WHERE user_id = %s
                """, (user_id,))

                for guild in bot.guilds:
                    member = guild.get_member(user_id)

                    if member:
                        await remove_all_streak_roles(member)

            conn.commit()

# ============================
# READY
# ============================

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")

# ============================
# MESSAGE TRACKING
# ============================

@bot.event
async def on_message(message):
    if message.author.bot or not message.guild:
        return

    now = datetime.now(timezone.utc)
    user_id = message.author.id

    with psycopg.connect(DB_URL) as conn:
        with conn.cursor() as cursor:

            cursor.execute("""
                SELECT msg_count, current_streak, last_streak_time
                FROM user_streaks
                WHERE user_id = %s
            """, (user_id,))

            row = cursor.fetchone()

            if not row:
                cursor.execute("""
                    INSERT INTO user_streaks
                    (user_id, msg_count, current_streak, last_msg_time)
                    VALUES (%s, 1, 0, %s)
                """, (user_id, now))

            else:
                msg_count, current_streak, last_streak_time = row
                msg_count += 1

                if msg_count >= 3:
                    if not last_streak_time or now - last_streak_time >= timedelta(hours=20):
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
                    WHERE user_id = %s
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
# COMMANDS
# ============================

@bot.tree.command(name="messagestreak")
async def messagestreak(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    with psycopg.connect(DB_URL) as conn:
        with conn.cursor() as cursor:
            cursor.execute("""
                SELECT current_streak
                FROM user_streaks
                WHERE user_id = %s
            """, (interaction.user.id,))
            row = cursor.fetchone()

    streak = row[0] if row else 0
    await interaction.followup.send(f"Your streak is **{streak}**")

@bot.tree.command(name="revivestreak")
async def revivestreak(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    now = datetime.now(timezone.utc)

    with psycopg.connect(DB_URL) as conn:
        with conn.cursor() as cursor:

            cursor.execute("""
                SELECT lost_streak, last_revive
                FROM user_streaks
                WHERE user_id = %s
            """, (interaction.user.id,))

            row = cursor.fetchone()

            if not row or row[0] == 0:
                await interaction.followup.send("No streak to revive.")
                return

            lost_streak, last_revive = row

            if last_revive and now - last_revive < timedelta(days=7):
                await interaction.followup.send("You can only revive once every 7 days.")
                return

            cursor.execute("""
                UPDATE user_streaks
                SET current_streak = %s,
                    lost_streak = 0,
                    last_revive = %s
                WHERE user_id = %s
            """, (lost_streak, now, interaction.user.id))

            conn.commit()

    await update_streak_roles(interaction.user, lost_streak)
    await interaction.followup.send(f"Revived streak to **{lost_streak}**")

@bot.tree.command(name="givestreak")
async def givestreak(interaction: discord.Interaction, user: discord.Member, amount: int):
    await interaction.response.defer(ephemeral=True)

    if not interaction.user.guild_permissions.administrator:
        await interaction.followup.send("Admins only.")
        return

    with psycopg.connect(DB_URL) as conn:
        with conn.cursor() as cursor:
            cursor.execute("""
                INSERT INTO user_streaks (user_id, current_streak)
                VALUES (%s, %s)
                ON CONFLICT (user_id)
                DO UPDATE SET current_streak = %s
            """, (user.id, amount, amount))
            conn.commit()

    await update_streak_roles(user, amount)
    await interaction.followup.send("Done.")

@bot.tree.command(name="removestreak")
async def removestreak(interaction: discord.Interaction, user: discord.Member):
    await interaction.response.defer(ephemeral=True)

    if not interaction.user.guild_permissions.administrator:
        await interaction.followup.send("Admins only.")
        return

    with psycopg.connect(DB_URL) as conn:
        with conn.cursor() as cursor:
            cursor.execute("""
                UPDATE user_streaks
                SET current_streak = 0,
                    msg_count = 0
                WHERE user_id = %s
            """, (user.id,))
            conn.commit()

    await remove_all_streak_roles(user)
    await interaction.followup.send("Removed streak.")

@bot.tree.command(name="channel")
async def channel(interaction: discord.Interaction, channel: discord.TextChannel):
    await interaction.response.defer(ephemeral=True)

    if not interaction.user.guild_permissions.manage_channels:
        await interaction.followup.send("Missing permission.")
        return

    with psycopg.connect(DB_URL) as conn:
        with conn.cursor() as cursor:
            cursor.execute("""
                INSERT INTO streak_config (guild_id, announcement_channel_id)
                VALUES (%s, %s)
                ON CONFLICT (guild_id)
                DO UPDATE SET announcement_channel_id = EXCLUDED.announcement_channel_id
            """, (interaction.guild_id, channel.id))
            conn.commit()

    await interaction.followup.send("Channel set.")

@bot.tree.command(name="adddreamteam")
async def adddreamteam(interaction: discord.Interaction, user: discord.Member):
    await interaction.response.defer(ephemeral=True)

    if not any(r.id == DREAM_TEAM_MANAGER_ROLE_ID for r in interaction.user.roles):
        await interaction.followup.send("No permission.")
        return

    await user.add_roles(interaction.guild.get_role(DREAM_TEAM_ROLE_ID))
    await interaction.followup.send("Role added.")

@bot.tree.command(name="removedreamteam")
async def removedreamteam(interaction: discord.Interaction, user: discord.Member):
    await interaction.response.defer(ephemeral=True)

    if not any(r.id == DREAM_TEAM_MANAGER_ROLE_ID for r in interaction.user.roles):
        await interaction.followup.send("No permission.")
        return

    await user.remove_roles(interaction.guild.get_role(DREAM_TEAM_ROLE_ID))
    await interaction.followup.send("Role removed.")

@bot.tree.command(name="addccrole")
async def addccrole(interaction: discord.Interaction, user: discord.Member):
    await interaction.response.defer(ephemeral=True)

    if not any(r.id == CC_MANAGER_ROLE_ID for r in interaction.user.roles):
        await interaction.followup.send("No permission.")
        return

    await user.add_roles(interaction.guild.get_role(CC_ROLE_ID))
    await interaction.followup.send("CC role added.")

@bot.tree.command(name="removeccrole")
async def removeccrole(interaction: discord.Interaction, user: discord.Member):
    await interaction.response.defer(ephemeral=True)

    if not any(r.id == CC_MANAGER_ROLE_ID for r in interaction.user.roles):
        await interaction.followup.send("No permission.")
        return

    await user.remove_roles(interaction.guild.get_role(CC_ROLE_ID))
    await interaction.followup.send("CC role removed.")

# ============================
# RUN
# ============================

token = os.environ.get("DISCORD_TOKEN")

if token:
    bot.run(token)
else:
    print("Missing DISCORD_TOKEN")
