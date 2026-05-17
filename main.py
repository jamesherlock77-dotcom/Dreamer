import os
import psycopg
import discord
from discord import app_commands
from discord.ext import commands

# 1. Set up intents (permissions) the bot needs
intents = discord.Intents.default()
intents.message_content = True 
intents.members = True  # Required to kick members

class MyBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=intents)
        
    async def setup_hook(self):
        # Synchronizes slash commands globally with Discord
        await self.tree.sync()

bot = MyBot()

# Configuration Variables
ALLOWED_USER_ID = 1429110753683832985
DB_URL = os.environ.get("DATABASE_URL")

# 2. Database Functions
def init_db():
    """Creates the table and config row in Postgres if they do not exist."""
    with psycopg.connect(DB_URL) as conn:
        with conn.cursor() as cursor:
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS deadtrap_settings (
                    id SERIAL PRIMARY KEY,
                    channel_id BIGINT,
                    kick_count INT DEFAULT 0
                );
            """)
            # Ensure a single tracking row with ID=1 exists
            cursor.execute("SELECT id FROM deadtrap_settings WHERE id = 1;")
            if not cursor.fetchone():
                cursor.execute("INSERT INTO deadtrap_settings (id, channel_id, kick_count) VALUES (1, NULL, 0);")
            conn.commit()

def get_settings():
    """Retrieves the current trap channel ID and kick count."""
    with psycopg.connect(DB_URL) as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT channel_id, kick_count FROM deadtrap_settings WHERE id = 1;")
            return cursor.fetchone()

def save_channel(channel_id):
    """Saves the channel ID and resets the kick count for a new setup."""
    with psycopg.connect(DB_URL) as conn:
        with conn.cursor() as cursor:
            cursor.execute("UPDATE deadtrap_settings SET channel_id = %s, kick_count = 0 WHERE id = 1;", (channel_id,))
            conn.commit()

def increment_kick_count():
    """Increments the kick count and returns the updated total."""
    with psycopg.connect(DB_URL) as conn:
        with conn.cursor() as cursor:
            cursor.execute("UPDATE deadtrap_settings SET kick_count = kick_count + 1 WHERE id = 1 RETURNING kick_count;")
            new_count = cursor.fetchone()[0]
            conn.commit()
            return new_count


@bot.event
async def on_ready():
    if not DB_URL:
        print("CRITICAL ERROR: DATABASE_URL environment variable is missing!")
        return
    init_db()  # Setup tables on startup
    print(f"Logged in as {bot.user.name}")
    print("PostgreSQL Database linked and bot is running on Railway!")


# 3. Slash Command to set the deadtrap channel
@bot.tree.command(name="deadtrap", description="Sets the channel to act as a deadtrap.")
@app_commands.describe(channel="The channel to turn into a deadtrap")
async def deadtrap(interaction: discord.Interaction, channel: discord.TextChannel):
    # Enforce permission restriction
    if interaction.user.id != ALLOWED_USER_ID:
        await interaction.response.send_message("You do not have permission to use this command.", ephemeral=True)
        return

    # Update state in PostgreSQL
    save_channel(channel.id)
    
    await interaction.response.send_message(f"Deadtrap active in {channel.mention}.", ephemeral=True)
    
    # Send the initial warning text to the targeted channel
    warning_text = (
        f"<:Sneeze:1495243609035899023> DONT TYPE IN HERE <:Sneeze:1495243609035899023>\n"
        f"You will be kicked\n"
        f"`Kick Count: 0`"
    )
    await channel.send(warning_text)


# 4. Message Monitor
@bot.event
async def on_message(message):
    # Ignore messages sent by bots
    if message.author.bot:
        return

    # Safely pull settings dynamically from the database
    trap_channel_id, current_count = get_settings()
    
    # Only act if the message was sent in the designated channel
    if trap_channel_id is None or message.channel.id != trap_channel_id:
        return

    # Delete their message instantly
    try:
        await message.delete()
    except discord.Forbidden:
        print("Bot lacks 'Manage Messages' permission.")
    except discord.NotFound:
        pass

    member = message.author
    
    # Direct Message warning attempt
    try:
        await member.send("You have been kicked from Dreamer Vr Server. For supposed hacked account.")
    except discord.Forbidden:
        print(f"Could not DM {member.name} (User has private settings enabled).")

    # Perform kick and update counter
    try:
        await member.kick(reason="Typed in the designated deadtrap channel.")
        
        # Safely increment inside Postgres
        updated_count = increment_kick_count()
        
        warning_text = (
            f"<:Sneeze:1495243609035899023> DONT TYPE IN HERE <:Sneeze:1495243609035899023>\n"
            f"You will be kicked\n"
            f"`Kick Count: {updated_count}`"
        )
        
        # Search back to find the warning message and edit it dynamically
        async for msg in message.channel.history(limit=20):
            if msg.author == bot.user and "DONT TYPE IN HERE" in msg.content:
                await msg.edit(content=warning_text)
                break
        else:
            # Fallback if the previous message can't be found
            await message.channel.send(warning_text)
            
    except discord.Forbidden:
        print(f"Failed to kick {member.name}. Check role hierarchy or 'Kick Members' permission.")

# Run the bot execution loop
token = os.environ.get("DISCORD_TOKEN")
if token:
    bot.run(token)
else:
    print("Error: DISCORD_TOKEN variable not found in the environment settings.")
