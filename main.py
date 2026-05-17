import os
import io
import psycopg
import requests
from datetime import datetime, timedelta, timezone
import discord
from discord import app_commands
from discord.ext import commands, tasks
from PIL import Image, ImageDraw, ImageFont

# Configuration
ALLOWED_USER_ID = 1429110753683832985
MANAGEMENT_CHANNEL_ID = 1505664564522651830
DB_URL = os.environ.get("DATABASE_URL")

# Role Map Configuration for Streak System (Streak Count: Role ID)
STREAK_ROLES = {
    1: 1495573627217641604,
    3: 1495573632984813639,
    7: 1495573635459448842,
    14: 1495573637800136844,
    30: 1495573640132034670,
    60: 1495573754921877687,
    100: 149557363004039380
}

# 1. Database Functions
def init_db():
    with psycopg.connect(DB_URL) as conn:
        with conn.cursor() as cursor:
            # Table for Deadtrap Settings
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS deadtrap_settings (
                    id INT PRIMARY KEY,
                    channel_id BIGINT,
                    kick_count INT DEFAULT 0
                );
            """)
            cursor.execute("SELECT id FROM deadtrap_settings WHERE id = 1;")
            if not cursor.fetchone():
                cursor.execute("INSERT INTO deadtrap_settings (id, channel_id, kick_count) VALUES (1, NULL, 0);")

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

            # Table for Global Bot Master Switch (ON/OFF Status)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS bot_status (
                    id INT PRIMARY KEY,
                    is_active BOOLEAN DEFAULT TRUE
                );
            """)
            cursor.execute("SELECT id FROM bot_status WHERE id = 1;")
            if not cursor.fetchone():
                cursor.execute("INSERT INTO bot_status (id, is_active) VALUES (1, TRUE);")

            conn.commit()

def get_bot_active_status():
    with psycopg.connect(DB_URL) as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT is_active FROM bot_status WHERE id = 1;")
            row = cursor.fetchone()
            return row[0] if row else True

def set_bot_active_status(status: bool):
    with psycopg.connect(DB_URL) as conn:
        with conn.cursor() as cursor:
            cursor.execute("UPDATE bot_status SET is_active = %s WHERE id = 1;", (status,))
            conn.commit()

def get_deadtrap_settings():
    with psycopg.connect(DB_URL) as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT channel_id, kick_count FROM deadtrap_settings WHERE id = 1;")
            return cursor.fetchone()

def save_deadtrap_channel(channel_id):
    with psycopg.connect(DB_URL) as conn:
        with conn.cursor() as cursor:
            cursor.execute("UPDATE deadtrap_settings SET channel_id = %s, kick_count = 0 WHERE id = 1;", (channel_id,))
            conn.commit()

def increment_kick_count():
    with psycopg.connect(DB_URL) as conn:
        with conn.cursor() as cursor:
            cursor.execute("UPDATE deadtrap_settings SET kick_count = kick_count + 1 WHERE id = 1 RETURNING kick_count;")
            new_count = cursor.fetchone()[0]
            conn.commit()
            return new_count

def build_deadtrap_embed(kick_count):
    embed = discord.Embed(
        title="DO NOT SEND MESSAGES IN THIS CHANNEL",
        description="This channel is used to catch spam bots. Any messages sent here will result in **a softban.**",
        color=discord.Color.from_rgb(43, 45, 49)
    )
    embed.add_field(name="\u200b", value=f"<:Dreamer:1495243686378868787> Kicks: {kick_count}", inline=False)
    return embed

def get_user_streak_and_rank(user_id):
    with psycopg.connect(DB_URL) as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT current_streak, msg_count FROM user_streaks WHERE user_id = %s;", (user_id,))
            user_row = cursor.fetchone()
            if not user_row:
                return 0, 0, 0
            
            streak_count, msg_progress = user_row

            cursor.execute("""
                SELECT position FROM (
                    SELECT user_id, RANK() OVER (ORDER BY current_streak DESC, last_msg_time ASC) as position 
                    FROM user_streaks
                ) as leaderboard WHERE user_id = %s;
            """, (user_id,))
            rank_row = cursor.fetchone()
            rank = rank_row[0] if rank_row else 0
            
            return streak_count, msg_progress, rank


# 2. Background Task Loop
@tasks.loop(seconds=30)
async def streak_expiry_check(bot_instance):
    if not get_bot_active_status():
        return

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
        print("CRITICAL: DATABASE_URL is missing!")
        return
    init_db()
    print(f"Logged in as {bot.user.name}")


# ==============================================================================
# SLASH COMMANDS
# ==============================================================================

@bot.tree.command(name="messagestreak", description="Displays your rank and message streak status card.")
async def messagestreak(interaction: discord.Interaction):
    if not get_bot_active_status():
        await interaction.response.send_message("The streak system is currently disabled by management.", ephemeral=True)
        return

    await interaction.response.defer()
    
    streak_count, msg_progress, rank_num = get_user_streak_and_rank(interaction.user.id)

    # 1. Base Dimensions (1000x230 matches the panoramic profile block size exactly)
    width, height = 1000, 230
    card = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(card)

    # 2. Precise Background Styling (Matches the translucent rounded slate look)
    card_bg_color = (17, 18, 20, 245)
    card_border_color = (38, 41, 45, 255)
    draw.rounded_rectangle([10, 10, width-10, height-10], radius=28, fill=card_bg_color, outline=card_border_color, width=2)
    
    # 3. Render Profile Picture + Gold Stroke Alignment
    avatar_size = 154
    avatar_x, avatar_y = 40, 38
    
    avatar_url = interaction.user.display_avatar.with_size(256).url
    try:
        response = requests.get(avatar_url, timeout=5)
        avatar_img = Image.open(io.BytesIO(response.content)).convert("RGBA")
        avatar_img = avatar_img.resize((avatar_size, avatar_size), Image.Resampling.LANCZOS)
        
        mask = Image.new("L", (avatar_size, avatar_size), 0)
        mask_draw = ImageDraw.Draw(mask)
        mask_draw.ellipse((0, 0, avatar_size, avatar_size), fill=255)
        
        card.paste(avatar_img, (avatar_x, avatar_y), mask=mask)
        
        # The sharp metallic gold frame ring
        draw.ellipse((avatar_x-1, avatar_y-1, avatar_x+avatar_size+1, avatar_y+avatar_size+1), outline=(197, 160, 89, 255), width=5)
    except Exception as e:
        print(f"Failed handling avatar asset: {e}")
        draw.ellipse((avatar_x-1, avatar_y-1, avatar_x+avatar_size+1, avatar_y+avatar_size+1), outline=(197, 160, 89, 255), width=5)

    # 4. Text Fonts Settings
    try:
        font_user = ImageFont.load_default(size=56)
        font_sub_label = ImageFont.load_default(size=24)
        font_stat_val = ImageFont.load_default(size=54)
    except TypeError:
        font_user = font_sub_label = font_stat_val = ImageFont.load_default()

    # Colors mapped exactly from the image example
    text_white = (255, 255, 255, 255)
    text_cyan = (95, 180, 195, 255) # Hex #5EB4C3 exact light cyan

    # 5. Draw Username (Positioned next to the avatar frame)
    username_text = interaction.user.display_name.lower() # Forced lower-case like 'imkirbs'
    draw.text((235, 36), username_text, font=font_user, fill=text_white)
    
    # 6. Aligned Metrics Grid Layout 
    # Current Streak Placement Block
    draw.text((235, 115), "Current Streak", font=font_sub_label, fill=text_white)
    draw.text((235, 148), f"{streak_count} Days", font=font_stat_val, fill=text_cyan)

    # Server Rank Placement Block (Pushed perfectly rightwards to decouple text fields)
    draw.text((615, 115), "Rank", font=font_sub_label, fill=text_white)
    draw.text((615, 148), f"#{rank_num}" if rank_num > 0 else "#--", font=font_stat_val, fill=text_cyan)

    # Save and stream output image configuration data
    final_buffer = io.BytesIO()
    card.save(final_buffer, format="PNG")
    final_buffer.seek(0)

    discord_file = discord.File(fp=final_buffer, filename="streak_card.png")
    await interaction.followup.send(file=discord_file)


@bot.tree.command(name="togglebot", description="Turn the entire bot functionality ON or OFF.")
@app_commands.describe(status="Choose True to turn ON, False to turn OFF")
async def togglebot(interaction: discord.Interaction, status: bool):
    if interaction.user.id != ALLOWED_USER_ID:
        await interaction.response.send_message("You do not have permission to use this command.", ephemeral=True)
        return
        
    if interaction.channel_id != MANAGEMENT_CHANNEL_ID:
        await interaction.response.send_message(f"This command can only be executed in <#{MANAGEMENT_CHANNEL_ID}>.", ephemeral=True)
        return

    set_bot_active_status(status)
    state_str = "🟢 **ENABLED/ONLINE**" if status else "🔴 **DISABLED/OFFLINE**"
    await interaction.response.send_message(f"The bot is now {state_str}. All activity metrics updates are completely paused.")

@bot.tree.command(name="deadtrap", description="Sets the channel to act as a deadtrap.")
@app_commands.describe(channel="The channel to turn into a deadtrap")
async def deadtrap(interaction: discord.Interaction, channel: discord.TextChannel):
    if interaction.user.id != ALLOWED_USER_ID:
        await interaction.response.send_message("You do not have permission to use this command.", ephemeral=True)
        return

    save_deadtrap_channel(channel.id)
    await interaction.response.send_message(f"Deadtrap channel active in {channel.mention}.", ephemeral=True)
    
    await channel.send(content="<:Sneeze:1495243609035899023> DONT TYPE IN HERE <:Sneeze:1495243609035899023>\nYou will be kicked")
    embed = build_deadtrap_embed(0)
    await channel.send(embed=embed)

@bot.tree.command(name="channel", description="Sets the channel where streak milestone alerts are sent.")
@app_commands.describe(channel="The channel for chat streak alerts")
async def channel_config(interaction: discord.Interaction, channel: discord.TextChannel):
    if interaction.user.id != ALLOWED_USER_ID:
        await interaction.response.send_message("You do not have permission to use this command.", ephemeral=True)
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
# CORE MESSAGING CONTROLLER (Deadtrap + Streaks)
# ==============================================================================

@bot.event
async def on_message(message):
    if message.author.bot or not message.guild:
        return

    if not get_bot_active_status():
        return

    deadtrap_channel_id, current_kicks = get_deadtrap_settings()

    # --- DEADTRAP TARGET BLOCK ---
    if deadtrap_channel_id and message.channel.id == deadtrap_channel_id:
        try:
            await message.delete()
        except discord.Forbidden:
            print("Bot lacks 'Manage Messages' permission.")
        except discord.NotFound:
            pass

        member = message.author
        
        try:
            await member.send("You have been kicked from Dreamer Vr Server. For supposed hacked account.")
        except discord.Forbidden:
            print(f"Could not DM {member.name}.")

        try:
            await member.kick(reason="Typed in the designated deadtrap channel.")
            updated_kicks = increment_kick_count()
            
            async for msg in message.channel.history(limit=20):
                if msg.author == bot.user and len(msg.embeds) > 0:
                    if "DO NOT SEND MESSAGES IN THIS CHANNEL" in msg.embeds[0].title:
                        new_embed = build_deadtrap_embed(updated_kicks)
                        await msg.edit(embed=new_embed)
                        break
            else:
                new_embed = build_deadtrap_embed(updated_kicks)
                await message.channel.send(embed=new_embed)
                
        except discord.Forbidden:
            print(f"Failed to kick {member.name}. Check role hierarchy rules.")
        
        return

    # --- STREAK MANAGEMENT BLOCK ---
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

            # 12-Hour Cooldown Check
            if last_streak_time:
                if now - last_streak_time < timedelta(hours=12):
                    conn.commit()
                    return

            new_msg_count = msg_count + 1

            # Check Milestone (3 messages)
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

# Run Bot
token = os.environ.get("DISCORD_TOKEN")
if token:
    bot.run(token)
else:
    print("Error: DISCORD_TOKEN is missing from variables configuration.")
