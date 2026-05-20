import os
import asyncio
import psycopg
import aiohttp
from datetime import datetime, timedelta, timezone
import base64

import discord
from discord import app_commands
from discord.ext import commands, tasks

# ============================
# CONFIG
# ============================

DB_URL = os.environ.get("DATABASE_URL")
HORDE_API_KEY = os.environ.get("HORDE_API_KEY")

STREAK_ROLES = {
    1: 1495573627217641604,
    3: 1495573632984813639,
    7: 1495573635459448842,
    14: 1495573637800136844,
    30: 1495573640132034670,
    60: 1495573754921877687,
    100: 1495573763004039380
}

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
# HORDE IMAGE GENERATION
# ============================

async def generate_streak_image(display_name, streak, rank):
    prompt = (
        f"Create a clean, modern streak card with a blurred colorful background, "
        f"a dark rounded rectangle panel, and bold high-contrast text. "
        f"Top text: '{display_name}'. "
        f"Middle text: 'Current Streak {streak} Days'. "
        f"Bottom text: 'Rank #{rank}'. "
        f"Match the exact style of the reference image: soft glow, vibrant blur, "
        f"sharp panel edges, centered layout, no avatar."
    )

    async with aiohttp.ClientSession() as session:
        async with session.post(
            "https://aihorde.net/api/v2/generate/async",
            json={
                "prompt": prompt,
                "params": {
                    "sampler_name": "k_euler",
                    "cfg_scale": 7,
                    "denoising_strength": 1,
                    "seed": "0",  # FIXED: must be a string
                    "steps": 20,
                    "width": 1024,
                    "height": 512,
                    "model": "SDXL-Lightning-v2"  # FIXED MODEL NAME
                }
            },
            headers={"apikey": HORDE_API_KEY}
        ) as resp:
            data = await resp.json()

        if "id" not in data:
            raise RuntimeError(f"Horde error (no id): {data}")

        job_id = data["id"]

        # Poll until ready
        while True:
            async with session.get(
                f"https://aihorde.net/api/v2/generate/status/{job_id}",
                headers={"apikey": HORDE_API_KEY}
            ) as resp:
                status = await resp.json()

            if status.get("done"):
                break

            await asyncio.sleep(1)

        # Retrieve final image
        async with session.get(
            f"https://aihorde.net/api/v2/generate/status/{job_id}",
            headers={"apikey": HORDE_API_KEY}
        ) as resp:
            result = await resp.json()

        if "generations" not in result or not result["generations"]:
            raise RuntimeError(f"Horde returned no generations: {result}")

        img_b64 = result["generations"][0]["img"]
        return base64.b64decode(img_b64)

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
        self.streak_task = streak_expiry_check.start(self)

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
                    SET current_streak = 0, msg_count = 0, last_streak_time = NULL 
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
# /streaks COMMAND
# ============================

@bot.tree.command(name="streaks", description="View your streak card.")
async def streaks(interaction: discord.Interaction):

    user_id = interaction.user.id

    with psycopg.connect(DB_URL) as conn:
        with conn.cursor() as cursor:

            cursor.execute("SELECT current_streak FROM user_streaks WHERE user_id = %s;", (user_id,))
            row = cursor.fetchone()

            if not row:
                await interaction.response.send_message(
                    "You don't have a streak yet.",
                    ephemeral=True
                )
                return

            streak = row[0]

            cursor.execute("""
                SELECT user_id, current_streak,
                RANK() OVER (ORDER BY current_streak DESC) AS r
                FROM user_streaks;
            """)
            ranks = cursor.fetchall()

            rank = next((r for uid, s, r in ranks if uid == user_id), None)

    display_name = interaction.user.display_name

    await interaction.response.defer()

    try:
        img_bytes = await generate_streak_image(display_name, streak, rank)
    except Exception as e:
        await interaction.followup.send(
            f"Failed to generate streak image: `{e}`",
            ephemeral=True
        )
        return

    await interaction.followup.send(
        file=discord.File(fp=img_bytes, filename="streak.png")
    )

# ============================
# MESSAGE HANDLER
# ============================

@bot.event
async def on_message(message):
    if message.author.bot or not message.guild:
        return

    user_id = message.author.id
    now = datetime.now(timezone.utc)

    with psycopg.connect(DB_URL) as conn:
        with conn.cursor() as cursor:

            cursor.execute(
                "SELECT msg_count, current_streak, last_streak_time FROM user_streaks WHERE user_id = %s;",
                (user_id,)
            )
            user_data = cursor.fetchone()

            if not user_data:
                cursor.execute("""
                    INSERT INTO user_streaks (user_id, msg_count, current_streak, last_msg_time)
                    VALUES (%s, 1, 0, %s);
                """, (user_id, now))
                conn.commit()
                await bot.process_commands(message)
                return

            msg_count, current_streak, last_streak_time = user_data

            cursor.execute(
                "UPDATE user_streaks SET last_msg_time = %s WHERE user_id = %s;",
                (now, user_id)
            )

            can_progress = (
                last_streak_time is None or
                now - last_streak_time >= timedelta(hours=20)
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

                cursor.execute(
                    "SELECT announcement_channel_id FROM streak_config WHERE guild_id = %s;",
                    (message.guild.id,)
                )
                config = cursor.fetchone()

                if config and config[0]:
                    channel = message.guild.get_channel(config[0])
                    if channel:
                        await channel.send(
                            f"<:Sneeze:1495243609035899023> {message.author.mention}, you've acquired a chat streak!\n"
                            f"**Streak:** `{new_streak}`"
                        )

                await update_streak_roles(message.author, new_streak)

            else:
                cursor.execute(
                    "UPDATE user_streaks SET msg_count = %s WHERE user_id = %s;",
                    (new_msg_count, user_id)
                )
                conn.commit()

    await bot.process_commands(message)

# ============================
# RUN BOT
# ============================

token = os.environ.get("DISCORD_TOKEN")
if token:
    bot.run(token)
else:
    print("Missing DISCORD_TOKEN")
