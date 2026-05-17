import os
import psycopg
from datetime import datetime, timedelta, timezone
import discord
from discord import app_commands
from discord.ext import commands, tasks

# Configuration
DB_URL = os.environ.get("DATABASE_URL")

# Role Map Configuration for Streak System (Streak Count: Role ID)
STREAK_ROLES = {
    1: 1495573627217641604,
    3: 1495573632984813639,
    7: 1495573635459448842,
    14: 1495573637800136844,
    30: 1495573640132034670,
    60: 1495573754921877687,
    100: 1495573763004039380
}

# 1. Database Functions
def init_db():
    with psycopg.connect(DB_URL) as conn:
        with conn.cursor() as cursor:
            # Table for Streak Config (Announcement Channel)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS streak_config (
                    guild_id BIGINT PRIMARY KEY,
                    announcement_channel_id BIGINT
                );
            """)

            # Table for User Streak tracking
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


# 2. Background Task Loop (Streak Expiry Checker)
@tasks.loop(seconds=30)
async def streak_expiry_check(bot_instance):
    now = datetime.now(timezone.utc)
    expiry_limit = timedelta(hours=20)

    with psycopg.connect(DB_URL) as conn:
        with conn.cursor() as cursor:
            cursor.execute("""
                SELECT user_id, current_streak FROM user_streaks 
                WHERE current_streak > 0 AND (last_msg_time < %s OR (last_streak_time IS NOT NULL AND last_streak_time < %s));
            """, (now - expiry_limit, now - expiry_limit))
            
            expired_users = cursor.fetchall()

            for user_id, old_streak in expired_users:
                cursor.execute("""
                    UPDATE user_streaks 
                    SET current_streak = 0, msg_count = 0, last_streak_time = NULL 
                    WHERE user_id = %s;
                """, (user_id,))
                
                for guild in bot_instance.guilds:
                    member = guild.get_member(user_id)
                    if member:
                        await remove_all_streak_roles(member)
                        
                        cursor.execute("SELECT announcement_channel_id FROM streak_config WHERE guild_id = %s;", (guild.id,))
                        config = cursor.fetchone()
                        if config and config[0]:
                            channel = guild.get_channel(config[0])
                            if channel:
                                await channel.send(f"💔 {member.mention}, you have lost your streak!")
            conn.commit()


# 3. Set up Intents & Bot Class
intents = discord.Intents.default()
intents.message_content = True
intents.members = True 

class UnifiedBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=intents)
        
    async def setup_hook(self):
        await self.tree.sync()
        self.streak_task = streak_expiry_check.start(self)

bot = UnifiedBot()


# 4. Helper Role Adjusters
async def update_streak_roles(member: discord.Member, streak_count: int):
    try:
        if streak_count in STREAK_ROLES:
            target_role_id = STREAK_ROLES[streak_count]
            role = member.guild.get_role(target_role_id)
            if role and role not in member.roles:
                await member.add_roles(role, reason="Reached a chat streak milestone!")
    except discord.Forbidden:
        print(f"Lacking permissions to adjust roles for {member.name}.")

async def remove_all_streak_roles(member: discord.Member):
    try:
        roles_to_remove = [member.guild.get_role(r_id) for r_id in STREAK_ROLES.values()]
        roles_to_remove = [r for r in roles_to_remove if r and r in member.roles]
        if roles_to_remove:
            await member.remove_roles(*roles_to_remove, reason="Lost chat streak due to inactivity.")
    except discord.Forbidden:
        print(f"Lacking permissions to strip roles from {member.name}.")


@bot.event
async def on_ready():
    if not DB_URL:
        print("CRITICAL: DATABASE_URL is missing from environment variables!")
        return
    init_db()
    print(f"Logged in and active as {bot.user.name}")


# ==============================================================================
# SLASH COMMANDS
# ==============================================================================

@bot.tree.command(name="channel", description="Sets the channel where streak milestone alerts are sent.")
@app_commands.describe(channel="The channel for chat streak alerts")
async def channel_config(interaction: discord.Interaction, channel: discord.TextChannel):
    if not interaction.user.guild_permissions.manage_channels:
        await interaction.response.send_message("You need 'Manage Channels' permissions to assign alert logs.", ephemeral=True)
        return

    with psycopg.connect(DB_URL) as conn:
        with conn.cursor() as cursor:
            cursor.execute("""
                INSERT INTO streak_config (guild_id, announcement_channel_id)
                VALUES (%s, %s)
                ON CONFLICT (guild_id) DO UPDATE SET announcement_channel_id = EXCLUDED.announcement_channel_id;
            """, (interaction.guild_id, channel.id))
            conn.commit()

    await interaction.response.send_message(f"Streak announcements will now be sent to {channel.mention}.", ephemeral=True)


# ==============================================================================
# CORE MESSAGING CONTROLLER (Streaks Tracking Engine)
# ==============================================================================

@bot.event
async def on_message(message):
    if message.author.bot or not message.guild:
        return

    user_id = message.author.id
    now = datetime.now(timezone.utc)

    with psycopg.connect(DB_URL) as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT msg_count, current_streak, last_streak_time FROM user_streaks WHERE user_id = %s;", (user_id,))
            user_data = cursor.fetchone()

            if not user_data:
                cursor.execute("""
                    INSERT INTO user_streaks (user_id, msg_count, current_streak, last_msg_time)
                    VALUES (%s, 1, 0, %s);
                """, (user_id, now))
                conn.commit()
                return
            
            msg_count, current_streak, last_streak_time = user_data

            cursor.execute("UPDATE user_streaks SET last_msg_time = %s WHERE user_id = %s;", (now, user_id))

            # 12-Hour Cooldown
            if last_streak_time:
                if now - last_streak_time < timedelta(hours=12):
                    conn.commit()
                    return

            new_msg_count = msg_count + 1

            # Check Progression Threshold Target (3 active messages)
            if new_msg_count >= 3:
                new_streak = current_streak + 1
                cursor.execute("""
                    UPDATE user_streaks 
                    SET msg_count = 0, current_streak = %s, last_streak_time = %s 
                    WHERE user_id = %s;
                """, (new_streak, now, user_id))
                conn.commit()

                cursor.execute("SELECT announcement_channel_id FROM streak_config WHERE guild_id = %s;", (message.guild.id,))
                config = cursor.fetchone()
                if config and config[0]:
                    announcement_channel = message.guild.get_channel(config[0])
                    if announcement_channel:
                        await announcement_channel.send(
                            f"<:Sneeze:1495243609035899023> {message.author.mention}, you've acquired a chat streak! <:Sneeze:1495243609035899023>\n"
                            f"**Streak:** `{new_streak}`"
                        )
                
                await update_streak_roles(message.author, new_streak)
            else:
                cursor.execute("UPDATE user_streaks SET msg_count = %s WHERE user_id = %s;", (new_msg_count, user_id))
                conn.commit()

# Run Bot Execution Hook
token = os.environ.get("DISCORD_TOKEN")
if token:
    bot.run(token)
else:
    print("Error: DISCORD_TOKEN configuration entry missing.")
