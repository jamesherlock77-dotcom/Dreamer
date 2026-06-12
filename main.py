import discord
from discord.ext import commands, tasks
from discord import app_commands
import os
from datetime import datetime, time
import pytz
from typing import Literal

# ── Config ───────────────────────────────────────────────────────────────────
TOKEN              = os.environ["DISCORD_BOT_TOKEN"]
DB_CHANNEL_ID      = 1515064641246466113   # message count database channel
LINK_CMD_CHANNEL   = 1513272619439226980   # only channel /link can be used in
LINK_LOG_CHANNEL   = 1512899799077093546   # approved links database channel
RESET_TIME         = time(23, 0)
RESET_WEEKDAY      = 6                     # Sunday
TIMEZONE           = pytz.timezone("UTC")

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
    if message.channel.id in (DB_CHANNEL_ID, 1500327292830875898):
        return

    db_channel = bot.get_channel(DB_CHANNEL_ID)
    if db_channel:
        await db_channel.send(f"{message.author.id}")

    # Image-only channel
    if message.channel.id == 1440105578839146517:
        has_image = any(
            a.content_type and a.content_type.startswith("image/")
            for a in message.attachments
        )
        if not has_image:
            await message.delete()
            return

    await bot.process_commands(message)

# ── Helper: read DB channel and tally counts ─────────────────────────────────
async def tally_counts() -> dict:
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

# ── /link ─────────────────────────────────────────────────────────────────────
@tree.command(name="link", description="Submit your YouTube or TikTok channel link for verification")
@app_commands.describe(
    platform="Your platform",
    url="Your YouTube or TikTok channel URL"
)
async def link(
    interaction: discord.Interaction,
    platform: Literal["YouTube", "TikTok"],
    url: str
):
    # Restrict to the designated channel
    if interaction.channel_id != LINK_CMD_CHANNEL:
        await interaction.response.send_message(
            f"This command can only be used in <#{LINK_CMD_CHANNEL}>.",
            ephemeral=True
        )
        return

    # Basic URL validation
    platform_lower = platform.lower()
    if platform_lower == "youtube" and "youtube.com" not in url and "youtu.be" not in url:
        await interaction.response.send_message(
            "That doesn't look like a valid YouTube URL.", ephemeral=True
        )
        return
    if platform_lower == "tiktok" and "tiktok.com" not in url:
        await interaction.response.send_message(
            "That doesn't look like a valid TikTok URL.", ephemeral=True
        )
        return

    # Send pending request with Accept / Deny buttons
    review_channel = bot.get_channel(LINK_LOG_CHANNEL)
    if not review_channel:
        await interaction.response.send_message("Could not reach the review channel.", ephemeral=True)
        return

    embed = discord.Embed(
        title="🔗 New Link Request",
        color=0xFFA500,
    )
    embed.add_field(name="User", value=interaction.user.mention, inline=True)
    embed.add_field(name="Platform", value=platform, inline=True)
    embed.add_field(name="URL", value=url, inline=False)
    embed.set_footer(text=f"User ID: {interaction.user.id}")

    view = LinkReviewView(
        submitter=interaction.user,
        platform=platform,
        url=url,
        log_channel_id=LINK_LOG_CHANNEL,
    )

    await review_channel.send(embed=embed, view=view)
    await interaction.response.send_message(
        "Your link has been submitted for review. You'll be notified once it's accepted or denied.",
        ephemeral=True
    )


class LinkReviewView(discord.ui.View):
    def __init__(self, submitter: discord.User, platform: str, url: str, log_channel_id: int):
        super().__init__(timeout=None)  # persistent until actioned
        self.submitter    = submitter
        self.platform     = platform
        self.url          = url
        self.log_channel_id = log_channel_id

    @discord.ui.button(label="Accept", style=discord.ButtonStyle.green)
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        log_channel = bot.get_channel(self.log_channel_id)

        # Log approved link as a clean entry in the database channel
        log_embed = discord.Embed(
            title="✅ Approved Link",
            color=0x808080,
        )
        log_embed.add_field(name="User", value=self.submitter.mention, inline=True)
        log_embed.add_field(name="Platform", value=self.platform, inline=True)
        log_embed.add_field(name="URL", value=self.url, inline=False)
        log_embed.set_footer(text=f"Approved by {interaction.user} • User ID: {self.submitter.id}")
        await log_channel.send(embed=log_embed)

        # Update the review message
        done_embed = discord.Embed(
            title="🔗 Link Request — Accepted",
            color=0x2ECC71,
        )
        done_embed.add_field(name="User", value=self.submitter.mention, inline=True)
        done_embed.add_field(name="Platform", value=self.platform, inline=True)
        done_embed.add_field(name="URL", value=self.url, inline=False)
        done_embed.set_footer(text=f"Accepted by {interaction.user}")
        await interaction.response.edit_message(embed=done_embed, view=None)

        # DM the submitter
        try:
            await self.submitter.send(
                f"Your {self.platform} link has been **accepted**!\n{self.url}"
            )
        except discord.Forbidden:
            pass

    @discord.ui.button(label="Deny", style=discord.ButtonStyle.red)
    async def deny(self, interaction: discord.Interaction, button: discord.ui.Button):
        done_embed = discord.Embed(
            title="🔗 Link Request — Denied",
            color=0xE74C3C,
        )
        done_embed.add_field(name="User", value=self.submitter.mention, inline=True)
        done_embed.add_field(name="Platform", value=self.platform, inline=True)
        done_embed.add_field(name="URL", value=self.url, inline=False)
        done_embed.set_footer(text=f"Denied by {interaction.user}")
        await interaction.response.edit_message(embed=done_embed, view=None)

        # DM the submitter
        try:
            await self.submitter.send(
                f"Your {self.platform} link submission was **denied**.\n{self.url}"
            )
        except discord.Forbidden:
            pass

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

    deleted = await db_channel.purge(limit=None)
    print(f"[{now}] Weekly reset — deleted {len(deleted)} log entries.")

# ── Startup ───────────────────────────────────────────────────────────────────
@bot.event
async def on_ready():
    await tree.sync()
    weekly_reset.start()
    print(f"Logged in as {bot.user} ({bot.user.id})")

bot.run(TOKEN)
