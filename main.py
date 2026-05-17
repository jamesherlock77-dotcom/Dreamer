import os
import discord
from discord import app_commands
from discord.ext import commands

# 1. Set up intents
intents = discord.Intents.default()
intents.message_content = True 
intents.members = True  # Required to kick members

class MyBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=intents)
        
    async def setup_hook(self):
        # This syncs the slash commands with Discord
        await self.tree.sync()

bot = MyBot()

# Configuration Variables
ALLOWED_USER_ID = 1429110753683832985
TRAP_CHANNEL_ID = None  # This will be set dynamically via the slash command
kick_count = 0

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user.name}")
    print("Bot is ready and running on Railway!")

# 2. Slash Command to set the deadtrap channel
@bot.tree.command(name="deadtrap", description="Sets the channel to act as a deadtrap.")
@app_commands.describe(channel="The channel to turn into a deadtrap")
async def deadtrap(interaction: discord.Interaction, channel: discord.TextChannel):
    global TRAP_CHANNEL_ID, kick_count
    
    # Check if the user is the allowed user ID
    if interaction.user.id != ALLOWED_USER_ID:
        await interaction.response.send_message("You do not have permission to use this command.", ephemeral=True)
        return

    # Update the target channel ID
    TRAP_CHANNEL_ID = channel.id
    kick_count = 0  # Reset counter for the new session
    
    await interaction.response.send_message(f"Deadtrap active in {channel.mention}.", ephemeral=True)
    
    # Send the warning message to the designated channel
    warning_text = (
        f"<:Sneeze:1495243609035899023> DONT TYPE IN HERE <:Sneeze:1495243609035899023>\n"
        f"You will be kicked\n"
        f"`Kick Count: {kick_count}`"
    )
    await channel.send(warning_text)

# 3. Event listener to monitor messages
@bot.event
async def on_message(message):
    global kick_count
    
    # Ignore bot messages and check if the message is in the setup trap channel
    if message.author.bot or TRAP_CHANNEL_ID is None or message.channel.id != TRAP_CHANNEL_ID:
        return

    # Delete their message immediately
    try:
        await message.delete()
    except discord.Forbidden:
        print("Bot lacks permission to delete messages.")
    except discord.NotFound:
        pass

    member = message.author
    
    # Try sending them a DM before kicking
    try:
        await member.send("You have been kicked from Dreamer Vr Server. For supposed hacked account.")
    except discord.Forbidden:
        print(f"Could not send DM to {member.name} (DMs are likely closed).")

    # Kick the user
    try:
        await member.kick(reason="Typed in the designated deadtrap channel.")
        kick_count += 1
        
        # Update the channel warning message with the new count
        warning_text = (
            f"<:Sneeze:1495243609035899023> DONT TYPE IN HERE <:Sneeze:1495243609035899023>\n"
            f"You will be kicked\n"
            f"`Kick Count: {kick_count}`"
        )
        
        # Find the last warning message sent by the bot to edit it, or send a new one
        async for msg in message.channel.history(limit=20):
            if msg.author == bot.user and "DONT TYPE IN HERE" in msg.content:
                await msg.edit(content=warning_text)
                break
        else:
            await message.channel.send(warning_text)
            
    except discord.Forbidden:
        print(f"Failed to kick {member.name}. The bot might lack the 'Kick Members' permission or its role is below the user's role.")

# Run the bot
token = os.environ.get("DISCORD_TOKEN")
if token:
    bot.run(token)
else:
    print("Error: DISCORD_TOKEN variable not found.")
