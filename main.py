import osq
import re
import asyncio
import aiohttp
import discord
from discord.ext import commands, tasks
from datetime import datetime, timezone, timedelta

# ── Config ──────────────────────────────────────────────────────────────
YOUTUBE_CHANNEL_HANDLE  = "dreamyvrofficial"
TIKTOK_USERNAME         = "dreamyvrofficial"
DISCORD_CHANNEL_ID      = 1512853017920143560
MEMBER_COUNT_CHANNEL_ID = 1512865382782865529
FORUM_CHANNEL_ID        = 1498288028630913055
REQUIRED_TAG_ID         = 1512877289900081305
SUBMISSIONS_CHANNEL_ID  = 1512877823168090173
CC_DATABASE_CHANNEL_ID  = 1512899799077093546
CC_ROLE_ID              = 1495165348654219344
LINKCC_CHANNEL_ID       = 1512954067327127632
SUBMIT_VIEWS_CHANNEL_ID = 1513272619439226980
MOD_REVIEW_CHANNEL_ID   = 1513276845104304278
ROLE_MENTION            = "<@&1512854249174863882>"
CHECK_INTERVAL          = 1800  # 30 minutes

DISCORD_TOKEN   = os.environ["DISCORD_TOKEN"]
YOUTUBE_API_KEY = os.environ["YOUTUBE_API_KEY"]

# ── Tier config ──────────────────────────────────────────────────────────
TIERS = [
    {"role_id": 1512870343310835974, "label": "Copper",   "level": 1},
    {"role_id": 1512871854308589648, "label": "Bronze",   "level": 2},
    {"role_id": 1512871100361478375, "label": "Metal",    "level": 3},
    {"role_id": 1512872854926917874, "label": "Gold",     "level": 4},
    {"role_id": 1512873532374122516, "label": "Platinum", "level": 5},
    {"role_id": 1512874309641699399, "label": "Level 6",  "level": 6},
]
# ────────────────────────────────────────────────────────────────────────

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)

last_youtube_video_id    = None
last_tiktok_video_id     = None
youtube_channel_id_cache = None
processed_forum_posts    = set()
cc_database              = {}

YOUTUBE_REGEX = re.compile(r'https?://(?:www\.)?(?:youtube\.com/watch\?v=|youtu\.be/)([\w-]+)')
TIKTOK_REGEX  = re.compile(r'https?://(?:www\.)?tiktok\.com/@([\w.]+)/video/(\d+)')


# ── Database channel parser ───────────────────────────────────────────────

async def load_cc_database():
    global cc_database
    channel = bot.get_channel(CC_DATABASE_CHANNEL_ID)
    if not channel:
        print("CC database channel not found!")
        return

    print("Loading CC database from channel history...")
    cc_database = {}

    async for message in channel.history(limit=1000, oldest_first=True):
        if not message.author.bot:
            continue

        content = message.content

        confirmed_match = re.search(
            r'✅ CONFIRMED \| USER_ID:(\d+) \| PLATFORM:(\w+) \| HANDLE:(.+)',
            content
        )
        if confirmed_match:
            user_id  = int(confirmed_match.group(1))
            platform = confirmed_match.group(2)
            handle   = confirmed_match.group(3).strip()
            if user_id not in cc_database:
                cc_database[user_id] = {}
            cc_database[user_id][platform] = {"handle": handle, "confirmed": True}
            continue

        declined_match = re.search(r'❌ DECLINED \| USER_ID:(\d+) \| PLATFORM:(\w+)', content)
        if declined_match:
            user_id  = int(declined_match.group(1))
            platform = declined_match.group(2)
            if user_id in cc_database:
                cc_database[user_id].pop(platform, None)
            continue

        pending_match = re.search(
            r'🕐 PENDING \| USER_ID:(\d+) \| PLATFORM:(\w+) \| HANDLE:(.+)',
            content
        )
        if pending_match:
            user_id  = int(pending_match.group(1))
            platform = pending_match.group(2)
            handle   = pending_match.group(3).strip()
            if user_id not in cc_database:
                cc_database[user_id] = {}
            if platform not in cc_database[user_id]:
                cc_database[user_id][platform] = {"handle": handle, "confirmed": False}

    print(f"Loaded {len(cc_database)} CC entries.")


# ── CC Confirm/Decline buttons ────────────────────────────────────────────

class CCConfirmView(discord.ui.View):
    def __init__(self, user_id: int, platform: str, handle: str):
        super().__init__(timeout=None)
        self.user_id  = user_id
        self.platform = platform
        self.handle   = handle

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.success, emoji="✅", custom_id="cc_confirm")
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.user.guild_permissions.manage_roles:
            await interaction.response.send_message("❌ You don't have permission to do this.", ephemeral=True)
            return

        if self.user_id not in cc_database:
            cc_database[self.user_id] = {}
        cc_database[self.user_id][self.platform] = {"handle": self.handle, "confirmed": True}

        for child in self.children:
            child.disabled = True

        db_channel = bot.get_channel(CC_DATABASE_CHANNEL_ID)
        if db_channel:
            await db_channel.send(
                f"✅ CONFIRMED | USER_ID:{self.user_id} | PLATFORM:{self.platform} | HANDLE:{self.handle}"
            )

        member  = interaction.guild.get_member(self.user_id)
        mention = member.mention if member else f"<@{self.user_id}>"

        await interaction.response.send_message(
            f"✅ Confirmed {mention}'s {self.platform} handle as `{self.handle}`!",
            ephemeral=True
        )
        await interaction.message.delete()

    @discord.ui.button(label="Decline", style=discord.ButtonStyle.danger, emoji="❌", custom_id="cc_decline")
    async def decline(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.user.guild_permissions.manage_roles:
            await interaction.response.send_message("❌ You don't have permission to do this.", ephemeral=True)
            return

        if self.user_id in cc_database:
            cc_database[self.user_id].pop(self.platform, None)

        for child in self.children:
            child.disabled = True

        db_channel = bot.get_channel(CC_DATABASE_CHANNEL_ID)
        if db_channel:
            await db_channel.send(f"❌ DECLINED | USER_ID:{self.user_id} | PLATFORM:{self.platform}")

        member  = interaction.guild.get_member(self.user_id)
        mention = member.mention if member else f"<@{self.user_id}>"

        await interaction.response.send_message(
            f"❌ Declined {mention}'s {self.platform} handle submission.",
            ephemeral=True
        )
        await interaction.message.delete()


# ── /linkcc command ───────────────────────────────────────────────────────

@bot.tree.command(name="linkcc", description="Link your YouTube or TikTok handle (you can link both)")
@discord.app_commands.describe(
    platform="Your platform (youtube or tiktok)",
    handle="Your channel/account handle (without @)"
)
@discord.app_commands.choices(platform=[
    discord.app_commands.Choice(name="YouTube", value="youtube"),
    discord.app_commands.Choice(name="TikTok",  value="tiktok"),
])
async def linkcc(interaction: discord.Interaction, platform: str, handle: str):
    if interaction.channel_id != LINKCC_CHANNEL_ID:
        await interaction.response.send_message(
            f"❌ You can only use this command in <#{LINKCC_CHANNEL_ID}>.", ephemeral=True
        )
        return

    role = interaction.guild.get_role(CC_ROLE_ID)
    if not role or role not in interaction.user.roles:
        await interaction.response.send_message(
            "❌ You don't have permission to use this command.", ephemeral=True
        )
        return

    handle = handle.lstrip("@").strip()

    if interaction.user.id not in cc_database:
        cc_database[interaction.user.id] = {}
    cc_database[interaction.user.id][platform] = {"handle": handle, "confirmed": False}

    db_channel = bot.get_channel(CC_DATABASE_CHANNEL_ID)
    if db_channel:
        view = CCConfirmView(
            user_id=interaction.user.id,
            platform=platform,
            handle=handle
        )
        await db_channel.send(
            f"🕐 PENDING | USER_ID:{interaction.user.id} | PLATFORM:{platform} | HANDLE:{handle}\n\n"
            f"{interaction.user.mention} has submitted their {platform} handle as `{handle}`\n"
            f"An admin/mod can confirm or decline below:",
            view=view
        )

    await interaction.response.send_message(
        f"✅ Your `{platform}` handle `{handle}` has been submitted for review!",
        ephemeral=True
    )


# ── Submit Views system ───────────────────────────────────────────────────

class SubmitViewsModal(discord.ui.Modal, title="Submit Your TikTok Views"):
    views_count = discord.ui.TextInput(
        label="Your total #dreamyvr views (last 30 days)",
        placeholder="e.g. 15000",
        required=True,
        max_length=20,
    )
    tier_claimed = discord.ui.TextInput(
        label="Which tier are you claiming?",
        placeholder="e.g. Gold (Level 4)",
        required=True,
        max_length=50,
    )
    screenshot_url = discord.ui.TextInput(
        label="Screenshot URL (upload to Discord, paste link)",
        placeholder="https://cdn.discordapp.com/...",
        required=True,
        max_length=500,
    )

    async def on_submit(self, interaction: discord.Interaction):
        mod_channel = bot.get_channel(MOD_REVIEW_CHANNEL_ID)
        if not mod_channel:
            await interaction.response.send_message("❌ Could not find mod review channel.", ephemeral=True)
            return

        embed = discord.Embed(
            title="📊 Views Submission",
            color=discord.Color.blurple(),
            timestamp=datetime.now(timezone.utc)
        )
        embed.add_field(name="Creator", value=interaction.user.mention, inline=True)
        embed.add_field(name="Views Claimed", value=self.views_count.value, inline=True)
        embed.add_field(name="Tier Claimed", value=self.tier_claimed.value, inline=True)
        embed.set_image(url=self.screenshot_url.value)
        embed.set_footer(text=f"User ID: {interaction.user.id}")

        view = ModReviewView(user_id=interaction.user.id)
        await mod_channel.send(embed=embed, view=view)

        await interaction.response.send_message(
            "✅ Your submission has been sent to the mods for review! You'll receive a role once approved.",
            ephemeral=True
        )


class SubmitViewsButton(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Submit Views",
        style=discord.ButtonStyle.primary,
        emoji="📊",
        custom_id="submit_views_button"
    )
    async def submit_views(self, interaction: discord.Interaction, button: discord.ui.Button):
        role = interaction.guild.get_role(CC_ROLE_ID)
        if not role or role not in interaction.user.roles:
            await interaction.response.send_message(
                "❌ You need the Creator Collective role to submit views.",
                ephemeral=True
            )
            return
        await interaction.response.send_modal(SubmitViewsModal())


class TierRoleSelect(discord.ui.Select):
    def __init__(self, user_id: int):
        self.target_user_id = user_id
        options = [
            discord.SelectOption(
                label=f"Level {tier['level']} — {tier['label']}",
                value=str(tier["role_id"]),
                emoji="🏅"
            )
            for tier in TIERS
        ]
        super().__init__(
            placeholder="Select a tier role to assign...",
            options=options,
            custom_id=f"tier_select_{user_id}"
        )

    async def callback(self, interaction: discord.Interaction):
        if not interaction.user.guild_permissions.manage_roles:
            await interaction.response.send_message("❌ You don't have permission.", ephemeral=True)
            return

        role_id = int(self.values[0])
        role    = interaction.guild.get_role(role_id)
        member  = interaction.guild.get_member(self.target_user_id)

        if not member:
            await interaction.response.send_message("❌ Could not find that member.", ephemeral=True)
            return
        if not role:
            await interaction.response.send_message("❌ Could not find that role.", ephemeral=True)
            return

        # Remove all other tier roles first
        tier_role_ids = {tier["role_id"] for tier in TIERS}
        roles_to_remove = [r for r in member.roles if r.id in tier_role_ids]
        if roles_to_remove:
            await member.remove_roles(*roles_to_remove)

        await member.add_roles(role)

        tier_label = next((t["label"] for t in TIERS if t["role_id"] == role_id), "Unknown")
        await interaction.response.send_message(
            f"✅ Gave {member.mention} the **{tier_label}** role!",
            ephemeral=True
        )


class ModReviewView(discord.ui.View):
    def __init__(self, user_id: int):
        super().__init__(timeout=None)
        self.user_id = user_id
        self.add_item(TierRoleSelect(user_id=user_id))

    @discord.ui.button(label="Approve", style=discord.ButtonStyle.success, emoji="✅", custom_id="mod_approve")
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.user.guild_permissions.manage_roles:
            await interaction.response.send_message("❌ You don't have permission.", ephemeral=True)
            return

        member  = interaction.guild.get_member(self.user_id)
        mention = member.mention if member else f"<@{self.user_id}>"

        for child in self.children:
            child.disabled = True
        await interaction.message.edit(view=self)

        await interaction.response.send_message(
            f"✅ Approved {mention}'s submission! Make sure to assign their tier role using the dropdown above.",
            ephemeral=True
        )

    @discord.ui.button(label="Deny", style=discord.ButtonStyle.danger, emoji="❌", custom_id="mod_deny")
    async def deny(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.user.guild_permissions.manage_roles:
            await interaction.response.send_message("❌ You don't have permission.", ephemeral=True)
            return

        member  = interaction.guild.get_member(self.user_id)
        mention = member.mention if member else f"<@{self.user_id}>"

        for child in self.children:
            child.disabled = True
        await interaction.message.edit(view=self)

        await interaction.response.send_message(
            f"❌ Denied {mention}'s submission.",
            ephemeral=True
        )

        # Try to DM the user
        if member:
            try:
                await member.send(
                    "❌ Your views submission was denied. Please make sure your screenshot clearly shows "
                    "your TikTok analytics with #dreamyvr views. Feel free to resubmit!"
                )
            except Exception:
                pass


# ── Post persistent submit views message ─────────────────────────────────

@bot.tree.command(name="setupsubmitviews", description="Post the Submit Views button [Admin only]")
async def setupsubmitviews(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ No permission.", ephemeral=True)
        return

    channel = bot.get_channel(SUBMIT_VIEWS_CHANNEL_ID)
    if not channel:
        await interaction.response.send_message("❌ Submit views channel not found.", ephemeral=True)
        return

    embed = discord.Embed(
        title="📊 Submit Your TikTok Views",
        description=(
            "Are you a Creator Collective member? Submit your TikTok analytics to claim your tier role!\n\n"
            "**How it works:**\n"
            "1. Open TikTok → Profile → Creator Tools → Analytics\n"
            "2. Screenshot your total views for the last 28 days\n"
            "3. Upload the screenshot to Discord and copy the link\n"
            "4. Click **Submit Views** below and fill in the form\n\n"
            "A mod will review your submission and assign your role!"
        ),
        color=discord.Color.blurple()
    )

    await channel.send(embed=embed, view=SubmitViewsButton())
    await interaction.response.send_message("✅ Submit views message posted!", ephemeral=True)


# ── Member count ─────────────────────────────────────────────────────────

async def update_member_count(guild: discord.Guild):
    channel = guild.get_channel(MEMBER_COUNT_CHANNEL_ID)
    if channel:
        await channel.edit(name=f"☁️・Members: {guild.member_count}")

@bot.event
async def on_member_join(member: discord.Member):
    await update_member_count(member.guild)

@bot.event
async def on_member_remove(member: discord.Member):
    await update_member_count(member.guild)


# ── Video description fetchers ───────────────────────────────────────────

async def get_youtube_description(session: aiohttp.ClientSession, video_id: str) -> str:
    url    = "https://www.googleapis.com/youtube/v3/videos"
    params = {"part": "snippet", "id": video_id, "key": YOUTUBE_API_KEY}
    try:
        async with session.get(url, params=params) as r:
            data  = await r.json()
            items = data.get("items", [])
            if items:
                snippet = items[0]["snippet"]
                tags    = " ".join(snippet.get("tags", []))
                return f"{snippet.get('title', '')} {snippet.get('description', '')} {tags}"
    except Exception as e:
        print(f"YouTube description fetch error: {e}")
    return ""


async def get_tiktok_description(session: aiohttp.ClientSession, video_id: str, username: str) -> str:
    url = f"https://www.tiktok.com/@{username}/video/{video_id}"
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0 Safari/537.36"
        )
    }
    try:
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as r:
            html  = await r.text()
            match = re.search(r'<meta name="description" content="([^"]*)"', html)
            if match:
                return match.group(1)
            return html
    except Exception as e:
        print(f"TikTok description fetch error: {e}")
    return ""


async def video_has_dreamyvr_tag(session: aiohttp.ClientSession, content: str) -> bool:
    for match in YOUTUBE_REGEX.finditer(content):
        video_id    = match.group(1)
        description = await get_youtube_description(session, video_id)
        if re.search(r'#dreamyvr', description, re.IGNORECASE):
            return True
    for match in TIKTOK_REGEX.finditer(content):
        username    = match.group(1)
        video_id    = match.group(2)
        description = await get_tiktok_description(session, video_id, username)
        if re.search(r'#dreamyvr', description, re.IGNORECASE):
            return True
    return False


def has_required_tag(thread: discord.Thread) -> bool:
    if not hasattr(thread, 'applied_tags'):
        return False
    return any(tag.id == REQUIRED_TAG_ID for tag in thread.applied_tags)

def has_video_link(content: str) -> bool:
    return bool(YOUTUBE_REGEX.search(content) or TIKTOK_REGEX.search(content))

def already_reacted(message: discord.Message) -> bool:
    for reaction in message.reactions:
        if str(reaction.emoji) == "👍":
            return reaction.me
    return False


# ── Forum processor ──────────────────────────────────────────────────────

async def process_thread(thread: discord.Thread, session: aiohttp.ClientSession):
    try:
        if not has_required_tag(thread):
            return
        messages = [msg async for msg in thread.history(limit=1, oldest_first=True)]
        if not messages:
            return
        first_message = messages[0]
        content       = first_message.content
        if not has_video_link(content):
            return
        if not await video_has_dreamyvr_tag(session, content):
            return
        if not already_reacted(first_message):
            await first_message.add_reaction("👍")
        if thread.id not in processed_forum_posts:
            submissions_channel = bot.get_channel(SUBMISSIONS_CHANNEL_ID)
            if submissions_channel:
                post_link = f"https://discord.com/channels/{thread.guild.id}/{thread.id}/{first_message.id}"
                await submissions_channel.send(
                    f"📹 New community video submission from {first_message.author.mention}!\n{post_link}"
                )
            processed_forum_posts.add(thread.id)
    except Exception as e:
        print(f"Error processing thread {thread.id}: {e}")


@tasks.loop(seconds=CHECK_INTERVAL)
async def check_forum():
    forum_channel = bot.get_channel(FORUM_CHANNEL_ID)
    if not forum_channel or not isinstance(forum_channel, discord.ForumChannel):
        print("Forum channel not found or not a ForumChannel!")
        return
    print(f"Checking forum... ({len(forum_channel.threads)} active threads)")
    async with aiohttp.ClientSession() as session:
        for thread in forum_channel.threads:
            await process_thread(thread, session)
            await asyncio.sleep(0.5)
        try:
            async for thread in forum_channel.archived_threads(limit=100):
                await process_thread(thread, session)
                await asyncio.sleep(0.5)
        except Exception as e:
            print(f"Error fetching archived threads: {e}")

@check_forum.before_loop
async def before_check_forum():
    await bot.wait_until_ready()


# ── YouTube channel helpers ──────────────────────────────────────────────

async def get_youtube_channel_id(session: aiohttp.ClientSession) -> str | None:
    global youtube_channel_id_cache
    if youtube_channel_id_cache:
        return youtube_channel_id_cache
    url    = "https://www.googleapis.com/youtube/v3/channels"
    params = {"part": "id", "forHandle": YOUTUBE_CHANNEL_HANDLE, "key": YOUTUBE_API_KEY}
    async with session.get(url, params=params) as r:
        data  = await r.json()
        items = data.get("items", [])
        if items:
            youtube_channel_id_cache = items[0]["id"]
            return youtube_channel_id_cache
    return None


async def get_latest_youtube_video(session: aiohttp.ClientSession):
    channel_id = await get_youtube_channel_id(session)
    if not channel_id:
        return None, None
    url    = "https://www.googleapis.com/youtube/v3/search"
    params = {
        "part": "snippet",
        "channelId": channel_id,
        "order": "date",
        "maxResults": 1,
        "type": "video",
        "key": YOUTUBE_API_KEY,
    }
    async with session.get(url, params=params) as r:
        data  = await r.json()
        items = data.get("items", [])
        if items:
            vid_id = items[0]["id"]["videoId"]
            return vid_id, f"https://www.youtube.com/watch?v={vid_id}"
    return None, None


async def get_latest_tiktok_video(session: aiohttp.ClientSession):
    url = f"https://www.tiktok.com/@{TIKTOK_USERNAME}"
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0 Safari/537.36"
        )
    }
    try:
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as r:
            html    = await r.text()
            matches = re.findall(r'/@' + TIKTOK_USERNAME + r'/video/(\d+)', html)
            if matches:
                vid_id = matches[0]
                return vid_id, f"https://www.tiktok.com/@{TIKTOK_USERNAME}/video/{vid_id}"
    except Exception as e:
        print(f"TikTok scrape error: {e}")
    return None, None


async def send_notification(video_url: str):
    channel = bot.get_channel(DISCORD_CHANNEL_ID)
    if channel:
        await channel.send(
            f"Hey everyone, we just posted a video! Go check it out!\n"
            f"{ROLE_MENTION}\n"
            f"{video_url}"
        )


@tasks.loop(seconds=CHECK_INTERVAL)
async def check_socials():
    global last_youtube_video_id, last_tiktok_video_id
    async with aiohttp.ClientSession() as session:
        yt_id, yt_url = await get_latest_youtube_video(session)
        if yt_id and yt_id != last_youtube_video_id:
            if last_youtube_video_id is not None:
                await send_notification(yt_url)
            last_youtube_video_id = yt_id
        tt_id, tt_url = await get_latest_tiktok_video(session)
        if tt_id and tt_id != last_tiktok_video_id:
            if last_tiktok_video_id is not None:
                await send_notification(tt_url)
            last_tiktok_video_id = tt_id

@check_socials.before_loop
async def before_check():
    await bot.wait_until_ready()


# ── /testsocialmedia command ─────────────────────────────────────────────

@bot.tree.command(name="testsocialmedia", description="Test the social media notification format")
async def testsocialmedia(interaction: discord.Interaction):
    await interaction.response.defer()
    async with aiohttp.ClientSession() as session:
        yt_id, yt_url = await get_latest_youtube_video(session)
        tt_id, tt_url = await get_latest_tiktok_video(session)
    lines = []
    if yt_url:
        lines.append(
            f"**YouTube** ✅\n"
            f"Hey everyone, we just posted a video! Go check it out!\n"
            f"{ROLE_MENTION}\n"
            f"{yt_url}"
        )
    else:
        lines.append("**YouTube** ❌ Could not fetch latest video.")
    if tt_url:
        lines.append(
            f"**TikTok** ✅\n"
            f"Hey everyone, we just posted a video! Go check it out!\n"
            f"{ROLE_MENTION}\n"
            f"{tt_url}"
        )
    else:
        lines.append("**TikTok** ❌ Could not fetch latest video.")
    await interaction.followup.send("\n\n".join(lines))


# ── Bot startup ──────────────────────────────────────────────────────────

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} slash command(s)")
    except Exception as e:
        print(f"Failed to sync commands: {e}")

    # Re-register persistent views
    bot.add_view(CCConfirmView(user_id=0, platform="youtube", handle=""))
    bot.add_view(SubmitViewsButton())
    bot.add_view(ModReviewView(user_id=0))

    await load_cc_database()

    for guild in bot.guilds:
        await update_member_count(guild)

    check_socials.start()
    check_forum.start()


bot.run(DISCORD_TOKEN)
