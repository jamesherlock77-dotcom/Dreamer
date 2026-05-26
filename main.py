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
            if role and role not in member.roles:
                if streak >= threshold:
                    await member.add_roles(role)

            if role and streak < threshold and role in member.roles:
                await member.remove_roles(role)
    except discord.Forbidden:
        pass


async def remove_all_streak_roles(member):
    try:
        roles = [
            member.guild.get_role(r)
            for r in STREAK_ROLES.values()
        ]
        roles = [r for r in roles if r and r in member.roles]

        if roles:
            await member.remove_roles(*roles)

    except discord.Forbidden:
        pass


# ============================
# STREAK EXPIRY LOOP (29 HOURS)
# ============================

@tasks.loop(seconds=30)
async def streak_expiry_check():
    now = datetime.now(timezone.utc)

    with psycopg.connect(DB_URL) as conn:
        with conn.cursor() as cursor:

            cursor.execute("""
                SELECT user_id
                FROM user_streaks
                WHERE current_streak > 0
                AND last_msg_time < %s;
            """, (now - timedelta(hours=29),))

            expired = cursor.fetchall()

            for (user_id,) in expired:

                cursor.execute("""
                    UPDATE user_streaks
                    SET lost_streak = current_streak,
                        current_streak = 0,
                        msg_count = 0,
                        last_streak_time = NULL
                    WHERE user_id = %s;
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
# MESSAGE SYSTEM
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
                conn.commit()
                await bot.process_commands(message)
                return

            msg_count, current_streak, last_streak_time = row

            cursor.execute("""
                UPDATE user_streaks
                SET last_msg_time = %s
                WHERE user_id = %s;
            """, (now, user_id))

            # 24h requirement
            can_progress = (
                last_streak_time is None or
                now - last_streak_time >= timedelta(hours=24)
            )

            new_msg_count = msg_count + 1

            if new_msg_count >= 3 and can_progress:
                new_streak = current_streak + 1

                cursor.execute("""
                    UPDATE user_streaks
                    SET msg_count = 0,
                        current_streak = %s,
                        last_streak_time = %s
                    WHERE user_id = %s;
                """, (new_streak, now, user_id))

                conn.commit()

                await update_streak_roles(message.author, new_streak)

                cursor.execute("""
                    SELECT announcement_channel_id
                    FROM streak_config
                    WHERE guild_id = %s;
                """, (message.guild.id,))

                config = cursor.fetchone()

                if config and config[0]:
                    channel = message.guild.get_channel(config[0])
                    if channel:
                        await channel.send(
                            f"<:Sneeze:1495243609035899023> {message.author.mention}, you've acquired a chat streak!\n"
                            f"**Streak:** `{new_streak}`"
                        )

            else:
                cursor.execute("""
                    UPDATE user_streaks
                    SET msg_count = %s
                    WHERE user_id = %s;
                """, (new_msg_count, user_id))

                conn.commit()

    await bot.process_commands(message)


# ============================
# /CHANNEL
# ============================

@bot.tree.command(
    name="channel",
    description="Set the channel for streak announcements."
)
async def channel(interaction: discord.Interaction, channel: discord.TextChannel):
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

    await interaction.response.send_message(f"Set to {channel.mention}", ephemeral=True)


# ============================
# /STREAK
# ============================

@bot.tree.command(
    name="messagestreak",
    description="Check your current streak."
)
async def messagestreak(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    with psycopg.connect(DB_URL) as conn:
        with conn.cursor() as cursor:
            cursor.execute("""
                SELECT current_streak
                FROM user_streaks
                WHERE user_id = %s;
            """, (interaction.user.id,))

            row = cursor.fetchone()

    streak = row[0] if row else 0
    await interaction.followup.send(f"You have a message streak of `{streak}`")


# ============================
# /GIVE STREAK (ADMIN)
# ============================

@bot.tree.command(
    name="givestreak",
    description="Give a user a streak (Admin only)."
)
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
                DO UPDATE SET current_streak = EXCLUDED.current_streak;
            """, (user.id, amount))
            conn.commit()

    await update_streak_roles(user, amount)
    await interaction.followup.send("Done.")


# ============================
# /REMOVE STREAK (ADMIN)
# ============================

@bot.tree.command(
    name="removestreak",
    description="Remove a user's streak (Admin only)."
)
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
                WHERE user_id = %s;
            """, (user.id,))
            conn.commit()

    await remove_all_streak_roles(user)
    await interaction.followup.send("Removed.")


# ============================
# /REVIVE STREAK
# ============================

@bot.tree.command(
    name="revivestreak",
    description="Revive your last lost streak (1 per 7 days)."
)
async def revivestreak(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    now = datetime.now(timezone.utc)

    with psycopg.connect(DB_URL) as conn:
        with conn.cursor() as cursor:

            cursor.execute("""
                SELECT lost_streak, last_revive
                FROM user_streaks
                WHERE user_id = %s;
            """, (interaction.user.id,))

            row = cursor.fetchone()

            if not row or row[0] <= 0:
                await interaction.followup.send("No streak to revive.")
                return

            lost, last_revive = row

            if last_revive and now - last_revive < timedelta(days=7):
                await interaction.followup.send("Cooldown: 7 days.")
                return

            cursor.execute("""
                UPDATE user_streaks
                SET current_streak = %s,
                    lost_streak = 0,
                    last_revive = %s
                WHERE user_id = %s;
            """, (lost, now, interaction.user.id))

            conn.commit()

    await update_streak_roles(interaction.user, lost)
    await interaction.followup.send(f"Revived `{lost}`")


# ============================
# CC COMMANDS
# ============================

@bot.tree.command(name="addccrole", description="Give CC role.")
async def addccrole(interaction: discord.Interaction, user: discord.Member):
    if not any(r.id == CC_MANAGER_ROLE_ID for r in interaction.user.roles):
        await interaction.response.send_message("No permission.", ephemeral=True)
        return

    role = interaction.guild.get_role(CC_ROLE_ID)
    await user.add_roles(role)
    await interaction.response.send_message("Done.", ephemeral=True)


@bot.tree.command(name="removeccrole", description="Remove CC role.")
async def removeccrole(interaction: discord.Interaction, user: discord.Member):
    if not any(r.id == CC_MANAGER_ROLE_ID for r in interaction.user.roles):
        await interaction.response.send_message("No permission.", ephemeral=True)
        return

    role = interaction.guild.get_role(CC_ROLE_ID)
    await user.remove_roles(role)
    await interaction.response.send_message("Done.", ephemeral=True)


# ============================
# DREAM TEAM
# ============================

@bot.tree.command(name="adddreamteam", description="Add Dream Team role.")
async def adddreamteam(interaction: discord.Interaction, user: discord.Member):
    if not any(r.id == DREAM_TEAM_MANAGER_ROLE_ID for r in interaction.user.roles):
        await interaction.response.send_message("No permission.", ephemeral=True)
        return

    role = interaction.guild.get_role(DREAM_TEAM_ROLE_ID)
    await user.add_roles(role)
    await interaction.response.send_message("Done.", ephemeral=True)


@bot.tree.command(name="removedreamteam", description="Remove Dream Team role.")
async def removedreamteam(interaction: discord.Interaction, user: discord.Member):
    if not any(r.id == DREAM_TEAM_MANAGER_ROLE_ID for r in interaction.user.roles):
        await interaction.response.send_message("No permission.", ephemeral=True)
        return

    role = interaction.guild.get_role(DREAM_TEAM_ROLE_ID)
    await user.remove_roles(role)
    await interaction.response.send_message("Done.", ephemeral=True)


# ============================
# RUN BOT
# ============================

token = os.environ.get("DISCORD_TOKEN")

if token:
    bot.run(token)
else:
    print("Missing DISCORD_TOKEN")
