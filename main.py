import asyncio
import threading
import os
import discord
from discord import app_commands
from discord.ext import commands
import json
import random
import logging
from dotenv import load_dotenv
from datetime import datetime, timezone, timedelta
import aiohttp
import aiofiles
from io import BytesIO
from PIL import Image, ImageDraw, ImageFont
from typing import Optional
import tempfile
import shutil
import aiosqlite
import base64
from flask import Flask, send_from_directory

# Flask setup for health check
app = Flask(__name__, static_folder='static')

@app.route('/')
@app.route('/health')
def health():
    return 'Bot is alive! 🌿', 200

@app.route('/favicon.ico')
def favicon():
    return send_from_directory(app.static_folder, 'favicon.ico', mimetype='image/vnd.microsoft.icon')

def run_flask():
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port, debug=False)

# Start Flask in a background thread
flask_thread = threading.Thread(target=run_flask, daemon=True)
flask_thread.start()

# Logging setup
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# Environment variables
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = os.getenv("GUILD_ID")
if GUILD_ID:
    GUILD_ID = int(GUILD_ID)
else:
    GUILD_ID = None
DEFAULT_PREFIX = os.getenv("PREFIX", "!")
DB_FILE = "bot_data.db"  # SQLite database file
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")  # Add to Render env vars
REPO = os.getenv("GITHUB_REPO", "your-username/bot-data")  # Add to Render env vars, e.g., "username/bot-data"

if not TOKEN:
    logger.error("DISCORD_TOKEN is not set in .env file")
    raise ValueError("DISCORD_TOKEN is required")

# Data structures
prefixes: dict[int, str] = {}
level_channels: dict[int, Optional[int]] = {}
xp_data: dict[int, dict[int, int]] = {}
mod_stats: dict[int, dict[int, dict[str, list]]] = {}
last_seen: dict[int, str] = {}
last_deleted_photo: dict[int, list[dict]] = {}
afk_cache: dict[int, dict] = {}
msg_cooldown: dict[int, dict[int, float]] = {}

# GitHub sync functions
async def download_db():
    if not GITHUB_TOKEN:
        logger.warning("GITHUB_TOKEN not set; starting with new DB")
        return
    url = f"https://raw.githubusercontent.com/{REPO}/main/{DB_FILE}"
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            if resp.status == 200:
                content = await resp.read()
                async with aiofiles.open(DB_FILE, "wb") as f:
                    await f.write(content)
                logger.info("Downloaded bot_data.db from GitHub")
            else:
                logger.warning("No existing bot_data.db found on GitHub; starting fresh")

async def upload_db():
    if not GITHUB_TOKEN:
        logger.warning("GITHUB_TOKEN not set; cannot upload DB")
        return
    if not os.path.exists(DB_FILE):
        logger.warning("DB file not found; nothing to upload")
        return
    async with aiofiles.open(DB_FILE, "rb") as f:
        content = await f.read()
    encoded = base64.b64encode(content).decode('utf-8')
    async with aiohttp.ClientSession() as session:
        headers = {"Authorization": f"token {GITHUB_TOKEN}"}
        url = f"https://api.github.com/repos/{REPO}/contents/{DB_FILE}"
        async with session.get(url, headers=headers) as resp:
            sha = (await resp.json()).get("sha") if resp.status == 200 else None
        data = {
            "message": "Update bot_data.db",
            "content": encoded,
            "sha": sha
        } if sha else {
            "message": "Create bot_data.db",
            "content": encoded
        }
        async with session.put(url, headers=headers, json=data) as resp:
            if resp.status in (200, 201):
                logger.info("Uploaded bot_data.db to GitHub")
            else:
                logger.error(f"Failed to upload DB: {await resp.text()}")

async def periodic_db_upload():
    while True:
        await upload_db()
        await asyncio.sleep(300)  # Every 5 minutes

# SQLite setup
async def init_db():
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                guild_id INTEGER PRIMARY KEY,
                prefix TEXT,
                level_channel INTEGER
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS xp (
                guild_id INTEGER,
                user_id INTEGER,
                xp INTEGER,
                PRIMARY KEY (guild_id, user_id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS mod_stats (
                guild_id INTEGER,
                user_id INTEGER,
                action TEXT,
                timestamp TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS last_seen (
                guild_id INTEGER PRIMARY KEY,
                timestamp TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS last_deleted_photo (
                guild_id INTEGER,
                author TEXT,
                content TEXT,
                image_url TEXT,
                timestamp TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS afk (
                user_id INTEGER PRIMARY KEY,
                reason TEXT,
                since TEXT
            )
        """)
        await db.commit()

# Settings management
async def load_settings():
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute("SELECT guild_id, prefix, level_channel FROM settings") as cursor:
            async for row in cursor:
                guild_id, prefix, level_channel = row
                prefixes[guild_id] = prefix or DEFAULT_PREFIX
                level_channels[guild_id] = level_channel

async def save_settings():
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("DELETE FROM settings")
        for guild_id in set(prefixes.keys()) | set(level_channels.keys()):
            await db.execute(
                "INSERT OR REPLACE INTO settings (guild_id, prefix, level_channel) VALUES (?, ?, ?)",
                (guild_id, prefixes.get(guild_id, DEFAULT_PREFIX), level_channels.get(guild_id))
            )
        await db.commit()

# XP handling
async def load_xp(guild_id: int):
    if guild_id in xp_data:
        return
    xp_data[guild_id] = {}
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute("SELECT user_id, xp FROM xp WHERE guild_id = ?", (guild_id,)) as cursor:
            async for row in cursor:
                xp_data[guild_id][row[0]] = row[1]

async def save_xp(guild_id: int):
    if guild_id not in xp_data:
        return
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("DELETE FROM xp WHERE guild_id = ?", (guild_id,))
        for user_id, xp in xp_data[guild_id].items():
            await db.execute(
                "INSERT OR REPLACE INTO xp (guild_id, user_id, xp) VALUES (?, ?, ?)",
                (guild_id, user_id, xp)
            )
        await db.commit()

def get_user_xp(guild_id: int, user_id: int) -> int:
    if guild_id not in xp_data:
        asyncio.create_task(load_xp(guild_id))
    return xp_data.get(guild_id, {}).get(user_id, 0)

def set_user_xp(guild_id: int, user_id: int, xp: int):
    if guild_id not in xp_data:
        asyncio.create_task(load_xp(guild_id))
    xp_data.setdefault(guild_id, {})[user_id] = max(0, xp)
    asyncio.create_task(save_xp(guild_id))

async def add_user_xp(guild_id: int, user_id: int, amount: int):
    current = get_user_xp(guild_id, user_id)
    old_level = get_level(current)
    new_xp = current + amount
    set_user_xp(guild_id, user_id, new_xp)
    new_level = get_level(new_xp)
    if new_level > old_level:
        await notify_level_up(guild_id, user_id, new_level)

def get_level(xp: int) -> int:
    if xp <= 0:
        return 0
    return int((-1 + (1 + 8 * xp / 100) ** 0.5) / 2)

def xp_for_level(level: int) -> int:
    return 50 * level * (level + 1)

def get_level_info(xp: int) -> tuple[int, int, int, float]:
    level = get_level(xp)
    xp_start = xp_for_level(level)
    xp_for_next = 100 * (level + 1)
    xp_in_level = xp - xp_start
    next_needed = xp_for_next - xp_in_level
    progress = (xp_in_level / xp_for_next) * 100 if xp_for_next > 0 else 0
    return level, xp_in_level, next_needed, progress

def progress_bar(progress: float, length: int = 10) -> str:
    filled = int((progress / 100) * length)
    return "█" * filled + "□" * (length - filled)

level_rewards = {5: "VIP", 10: "Premium", 20: "Moderator"}

async def notify_level_up(guild_id: int, user_id: int, new_level: int):
    guild = bot.get_guild(guild_id)
    if not guild:
        return
    member = guild.get_member(user_id)
    if not member:
        return
    channel_id = level_channels.get(guild_id)
    if not channel_id:
        return
    channel = guild.get_channel(channel_id)
    if not channel:
        return
    reward_msg = ""
    if new_level in level_rewards:
        role_name = level_rewards[new_level]
        role = discord.utils.get(guild.roles, name=role_name)
        if not role:
            role = await guild.create_role(name=role_name, colour=discord.Color.random(), reason="Level reward")
        await member.add_roles(role, reason=f"Reached level {new_level}")
        reward_msg = f"\nUnlocked **{role_name}** role!"
    embed = discord.Embed(
        title="Level Up!",
        description=f"{member.mention} reached **Level {new_level}**!{reward_msg}",
        color=discord.Color.green(),
        timestamp=datetime.now(timezone.utc)
    )
    embed.set_thumbnail(url=member.display_avatar.url)
    await channel.send(embed=embed)

# Mod stats handling
async def load_mod_stats(guild_id: int):
    mod_stats[guild_id] = {}
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute("SELECT user_id, action, timestamp FROM mod_stats WHERE guild_id = ?", (guild_id,)) as cursor:
            async for row in cursor:
                user_id, action, timestamp = row
                mod_stats[guild_id].setdefault(user_id, {
                    "commands": [], "warned": [], "kicked": [], "banned": [], "unbanned": [],
                    "timed_out": [], "untimed_out": [], "jailed": [], "unjailed": []
                })[action].append(timestamp)

async def save_mod_stats(guild_id: int):
    if guild_id not in mod_stats:
        return
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("DELETE FROM mod_stats WHERE guild_id = ?", (guild_id,))
        for user_id, actions in mod_stats[guild_id].items():
            for action, timestamps in actions.items():
                for timestamp in timestamps:
                    await db.execute(
                        "INSERT INTO mod_stats (guild_id, user_id, action, timestamp) VALUES (?, ?, ?, ?)",
                        (guild_id, user_id, action, timestamp)
                    )
        await db.commit()

def update_mod_stats(guild_id: int, user_id: int, action: str):
    mod_stats_guild = mod_stats.setdefault(guild_id, {})
    mod_stats_user = mod_stats_guild.setdefault(user_id, {
        "commands": [], "warned": [], "kicked": [], "banned": [], "unbanned": [], "timed_out": [], "untimed_out": [], "jailed": [], "unjailed": []
    })
    mod_stats_user[action].append(datetime.now(timezone.utc).isoformat())
    asyncio.create_task(save_mod_stats(guild_id))

# Last seen handling
async def load_last_seen():
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute("SELECT guild_id, timestamp FROM last_seen") as cursor:
            async for row in cursor:
                last_seen[row[0]] = row[1]

async def save_last_seen():
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("DELETE FROM last_seen")
        for guild_id, timestamp in last_seen.items():
            await db.execute(
                "INSERT OR REPLACE INTO last_seen (guild_id, timestamp) VALUES (?, ?)",
                (guild_id, timestamp)
            )
        await db.commit()

# Last deleted photo handling
async def load_last_deleted_photo(guild_id: int):
    last_deleted_photo[guild_id] = []
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute(
            "SELECT author, content, image_url, timestamp FROM last_deleted_photo WHERE guild_id = ? ORDER BY timestamp DESC LIMIT 10",
            (guild_id,)
        ) as cursor:
            async for row in cursor:
                last_deleted_photo[guild_id].append({
                    "author": row[0], "content": row[1], "image_url": row[2], "timestamp": row[3]
                })

async def save_last_deleted_photo(guild_id: int):
    if guild_id not in last_deleted_photo:
        return
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("DELETE FROM last_deleted_photo WHERE guild_id = ?", (guild_id,))
        for photo in last_deleted_photo[guild_id][:10]:
            await db.execute(
                "INSERT INTO last_deleted_photo (guild_id, author, content, image_url, timestamp) VALUES (?, ?, ?, ?, ?)",
                (guild_id, photo["author"], photo["content"], photo["image_url"], photo["timestamp"])
            )
        await db.commit()

# AFK handling
async def load_afk():
    global afk_cache
    afk_cache.clear()
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute("SELECT user_id, reason, since FROM afk") as cursor:
            async for row in cursor:
                afk_cache[row[0]] = {"reason": row[1], "since": row[2]}

async def save_afk():
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("DELETE FROM afk")
        for user_id, data in afk_cache.items():
            await db.execute(
                "INSERT OR REPLACE INTO afk (user_id, reason, since) VALUES (?, ?, ?)",
                (user_id, data["reason"], data["since"])
            )
        await db.commit()

# Intents
intents = discord.Intents.default()
intents.members = True
intents.message_content = True
intents.guilds = True

bot = commands.Bot(command_prefix=lambda bot_, msg: commands.when_mentioned_or(prefixes.get(msg.guild.id, DEFAULT_PREFIX) if msg.guild else DEFAULT_PREFIX)(bot_, msg), intents=intents, help_command=None)
tree = bot.tree

# Helpers
async def has_permission(interaction_or_ctx, perm: str) -> bool:
    user = getattr(interaction_or_ctx, "user", interaction_or_ctx.author)
    if getattr(user.guild_permissions, perm, False):
        return True
    embed = discord.Embed(title="❌ Permission Denied", description=f"Requires `{perm}` permission.", color=discord.Color.red())
    embed.set_footer(text="Contact an admin if you believe this is an error.")
    await (interaction_or_ctx.response.send_message(embed=embed, ephemeral=True) if hasattr(interaction_or_ctx, "response") else interaction_or_ctx.send(embed=embed, delete_after=10))
    return False

async def _download_image_bytes(url: str) -> Optional[bytes]:
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url) as response:
                if response.status == 200:
                    return await response.read()
        except Exception as e:
            logger.error(f"Failed to download image from {url}: {e}")
            return None

def _wrap_text(text: str, font: ImageFont.FreeTypeFont, max_w: int) -> list[str]:
    dummy = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    words, lines, cur = text.split(), [], ""
    for w in words:
        test = f"{cur} {w}".strip()
        bbox = dummy.textbbox((0, 0), test, font=font)
        if (bbox[2] - bbox[0]) <= max_w:
            cur = test
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines

async def get_jailed_role(guild: discord.Guild) -> discord.Role:
    jailed = discord.utils.get(guild.roles, name="Jailed")
    if not jailed:
        jailed = await guild.create_role(name="Jailed", reason="Auto-jailed role", color=discord.Color.dark_grey())
        for ch in guild.channels:
            await ch.set_permissions(jailed, send_messages=False, speak=False, add_reactions=False)
    return jailed

async def send_dm(member: discord.Member, action: str, mod: discord.Member, reason: Optional[str]):
    try:
        embed = discord.Embed(title=f"🚨 You have been {action}", color=discord.Color.orange(), timestamp=datetime.now(timezone.utc))
        embed.add_field(name="Moderator", value=f"{mod} ({mod.id})", inline=False)
        embed.add_field(name="Reason", value=reason or "No reason provided", inline=False)
        embed.set_footer(text=f"Guild: {member.guild.name}", icon_url=member.guild.icon.url if member.guild.icon else None)
        await member.send(embed=embed)
    except discord.Forbidden:
        logger.warning(f"Failed to send DM to {member.id} for {action}")

async def mod_action_embed(target: discord.abc.User, action: str, reason: Optional[str], mod: discord.Member):
    color = discord.Color.red() if action in {"kick", "ban"} else discord.Color.orange()
    embed = discord.Embed(
        title=f"{action.capitalize()} Executed ⚡",
        color=color,
        timestamp=datetime.now(timezone.utc)
    )
    embed.add_field(name="Member", value=f"{target} ({target.id})", inline=True)
    embed.add_field(name="Moderator", value=f"{mod} ({mod.id})", inline=True)
    embed.add_field(name="Reason", value=reason or "No reason provided", inline=False)
    embed.set_thumbnail(url=target.display_avatar.url if hasattr(target, 'display_avatar') else None)
    embed.set_footer(text="Moderation action logged.", icon_url="https://i.imgur.com/3J2N8fI.png")  # Replace with a suitable icon URL
    return embed

# Command handlers
async def setprefix_handler(interaction_or_ctx, new_prefix: str):
    guild = interaction_or_ctx.guild
    if not guild:
        return await interaction_or_ctx.response.send_message("This command requires a server.", ephemeral=True)
    if not await has_permission(interaction_or_ctx, "manage_guild"):
        return
    if len(new_prefix) > 10:
        return await interaction_or_ctx.response.send_message("Prefix must be ≤10 characters.", ephemeral=True)
    prefixes[guild.id] = new_prefix
    await save_settings()
    embed = discord.Embed(title="✅ Prefix Updated", description=f"New prefix: `{new_prefix}`\n\nAll commands will now use this prefix.", color=discord.Color.green())
    embed.set_footer(text="Change it anytime with /setprefix!")
    await (interaction_or_ctx.response.send_message(embed=embed) if hasattr(interaction_or_ctx, "response") else interaction_or_ctx.send(embed=embed))

async def getprefix_handler(interaction_or_ctx):
    guild = interaction_or_ctx.guild
    prefix = DEFAULT_PREFIX if not guild else prefixes.get(guild.id, DEFAULT_PREFIX)
    embed = discord.Embed(title="📌 Current Prefix", description=f"Prefix: `{prefix}`\nSlash commands: `/`\n\nUse /setprefix to change it (admin only).", color=discord.Color.blue())
    embed.set_footer(text="Prefixes are server-specific.")
    await (interaction_or_ctx.response.send_message(embed=embed) if hasattr(interaction_or_ctx, "response") else interaction_or_ctx.send(embed=embed))

async def ping_handler(interaction_or_ctx):
    latency = round(bot.latency * 1000)
    color = discord.Color.green() if latency < 100 else discord.Color.orange() if latency < 200 else discord.Color.red()
    embed = discord.Embed(title="🏓 Pong!", description=f"Latency: **{latency}ms**\n\nBot is responsive!", color=color)
    embed.set_footer(text="Ping tested at " + datetime.now(timezone.utc).strftime("%H:%M UTC"))
    await (interaction_or_ctx.response.send_message(embed=embed) if hasattr(interaction_or_ctx, "response") else interaction_or_ctx.send(embed=embed))

async def help_handler(interaction_or_ctx):
    user = getattr(interaction_or_ctx, "user", interaction_or_ctx.author)
    guild = interaction_or_ctx.guild
    is_admin = guild and (user.guild_permissions.kick_members or user.guild_permissions.ban_members or user.guild_permissions.manage_guild or user == guild.owner)
    prefix = prefixes.get(guild.id if guild else 0, DEFAULT_PREFIX)
    embed = discord.Embed(title="🛠️ Command Guide", description=f"Use `{prefix}` for prefix commands or `/` for slash commands.\n\nType a command for more info!", color=discord.Color.from_rgb(0, 170, 255), timestamp=datetime.now(timezone.utc))
    embed.set_thumbnail(url="https://i.imgur.com/5BFecvA.png")  # Replace with a bot icon or help image
    general_cmds = [
        ("ping", "Bot latency check 🏓"),
        ("help", "Show this guide 📚"),
        ("afk [reason]", "Set AFK status 😴"),
        ("inrole [role]", "List role members 👥"),
        ("userinfo [member]", "User profile info 👤"),
        ("serverinfo", "Server details 🖥️"),
        ("avatar [member]", "User avatar image 🖼️"),
        ("banner [member]", "User banner image 📸"),
        ("quote <text> [member]", "Create a quote image 💬"),
        ("modstats [user]", "Moderation stats 📊"),
        ("getprefix", "Show current prefix 🔑"),
        ("rank [user]", "Show level and XP 🏅"),
        ("leaderboard", "Top users leaderboard 🏆"),
        ("rewards", "Level rewards list 🎁"),
        ("meme [keywords]", "Random meme 😂"),
        ("coinflip", "Flip a coin 🪙"),
        ("dice", "Roll a die 🎲"),
        ("showlm [number]", "View deleted photo 🗑️"),
        ("me", "Your profile summary 📝")
    ]
    embed.add_field(name="🌟 General Commands", value="\n".join(f"**`{cmd}`** - {desc}" for cmd, desc in general_cmds), inline=False)
    if is_admin:
        admin_cmds = [
            ("kick <member> [reason]", "Kick user 👢"),
            ("ban <member> [reason]", "Ban user 🔨"),
            ("unban <user> [reason]", "Unban user 🔓"),
            ("warn <member> [reason]", "Warn user ⚠️"),
            ("timeout <member> <duration> [reason]", "Timeout user ⏳"),
            ("untimeout <member> [reason]", "Remove timeout ⏰"),
            ("jail <member> [reason]", "Jail user 🔒"),
            ("unjail <member> [reason]", "Unjail user 🗝️"),
            ("setprefix <prefix>", "Change prefix 🔧"),
            ("purge <amount>", "Delete messages 🧹"),
            ("lock", "Lock channel 🚫"),
            ("unlock", "Unlock channel ✅"),
            ("xp_add <user> <amount>", "Add XP 📈"),
            ("xp_remove <user> <amount>", "Remove XP 📉"),
            ("level_set <user> <level>", "Set level ⚙️"),
            ("levelchannelset <channel>", "Set level channel 📢")
        ]
        embed.add_field(name="🔰 Admin Commands", value="\n".join(f"**`{cmd}`** - {desc}" for cmd, desc in admin_cmds), inline=False)
    embed.set_footer(text="Bot by xAI | Use responsibly!", icon_url="https://i.imgur.com/klY5xCe.png")
    await (interaction_or_ctx.response.send_message(embed=embed) if hasattr(interaction_or_ctx, "response") else interaction_or_ctx.send(embed=embed))

async def purge_handler(interaction_or_ctx, amount: int):
    if not interaction_or_ctx.guild:
        return
    if not await has_permission(interaction_or_ctx, "manage_messages"):
        return
    if amount < 1 or amount > 100:
        return await interaction_or_ctx.response.send_message("Amount must be 1-100.", ephemeral=True)
    channel = interaction_or_ctx.channel
    deleted = await channel.purge(limit=amount)
    embed = discord.Embed(title="🧹 Messages Purged", description=f"Deleted {len(deleted)} message(s).\n\nChannel cleaned up!", color=discord.Color.green())
    embed.set_footer(text="Purged at " + datetime.now(timezone.utc).strftime("%H:%M UTC"))
    await (interaction_or_ctx.response.send_message(embed=embed, ephemeral=True) if hasattr(interaction_or_ctx, "response") else interaction_or_ctx.send(embed=embed, delete_after=10))

async def lock_handler(interaction_or_ctx):
    if not interaction_or_ctx.guild:
        return
    if not await has_permission(interaction_or_ctx, "manage_channels"):
        return
    channel = interaction_or_ctx.channel
    await channel.set_permissions(interaction_or_ctx.guild.default_role, send_messages=False)
    embed = discord.Embed(title="🔒 Channel Locked", description=f"{channel.mention} is now locked.\nNo messages can be sent by @everyone.", color=discord.Color.orange())
    embed.set_footer(text="Use /unlock to reopen.")
    await (interaction_or_ctx.response.send_message(embed=embed) if hasattr(interaction_or_ctx, "response") else interaction_or_ctx.send(embed=embed))

async def unlock_handler(interaction_or_ctx):
    if not interaction_or_ctx.guild:
        return
    if not await has_permission(interaction_or_ctx, "manage_channels"):
        return
    channel = interaction_or_ctx.channel
    await channel.set_permissions(interaction_or_ctx.guild.default_role, send_messages=None)
    embed = discord.Embed(title="✅ Channel Unlocked", description=f"{channel.mention} is now unlocked.\nMessaging restored for @everyone.", color=discord.Color.green())
    embed.set_footer(text="Channel open!")
    await (interaction_or_ctx.response.send_message(embed=embed) if hasattr(interaction_or_ctx, "response") else interaction_or_ctx.send(embed=embed))

async def rank_handler(interaction_or_ctx, member: Optional[discord.Member] = None):
    if not interaction_or_ctx.guild:
        return
    target = member or getattr(interaction_or_ctx, "user", interaction_or_ctx.author)
    xp = get_user_xp(interaction_or_ctx.guild.id, target.id)
    level, xp_in_level, next_needed, progress = get_level_info(xp)
    embed = discord.Embed(title=f"🏅 {target.display_name}'s Rank", color=discord.Color.from_rgb(255, 215, 0), timestamp=datetime.now(timezone.utc))
    embed.add_field(name="Level", value=f"**{level}** 🎖️", inline=True)
    embed.add_field(name="XP", value=f"**{xp_in_level} / {xp_in_level + next_needed}** 📊", inline=True)
    embed.add_field(name="Progress to Next Level", value=f"{progress_bar(progress)} **{progress:.1f}%** 🚀", inline=False)
    embed.set_thumbnail(url=target.display_avatar.url)
    embed.set_footer(text="Earn XP by chatting! Next reward at Level 5.")
    await (interaction_or_ctx.response.send_message(embed=embed) if hasattr(interaction_or_ctx, "response") else interaction_or_ctx.send(embed=embed))

async def leaderboard_handler(interaction_or_ctx):
    if not interaction_or_ctx.guild:
        return
    guild_id = interaction_or_ctx.guild.id
    await load_xp(guild_id)
    user_xps = [(interaction_or_ctx.guild.get_member(uid), x) for uid, x in xp_data[guild_id].items() if interaction_or_ctx.guild.get_member(uid)]
    sorted_users = sorted(user_xps, key=lambda x: x[1], reverse=True)[:10]
    if not sorted_users:
        embed = discord.Embed(title="🏆 Leaderboard", description="No rankings yet. Start chatting to earn XP!", color=discord.Color.gold())
        return await (interaction_or_ctx.response.send_message(embed=embed) if hasattr(interaction_or_ctx, "response") else interaction_or_ctx.send(embed=embed))
    desc = ""
    for i, (m, x) in enumerate(sorted_users):
        medal = "🥇" if i == 0 else "🥈" if i == 1 else "🥉" if i == 2 else f"{i+1}."
        desc += f"{medal} **{m.display_name}** - {x} XP (Lv. {get_level(x)})\n"
    embed = discord.Embed(title="🏆 Server Leaderboard", description=desc, color=discord.Color.gold(), timestamp=datetime.now(timezone.utc))
    embed.set_footer(text="Top 10 users | Climb the ranks!")
    await (interaction_or_ctx.response.send_message(embed=embed) if hasattr(interaction_or_ctx, "response") else interaction_or_ctx.send(embed=embed))

async def xp_add_handler(interaction_or_ctx, member: discord.Member, amount: int):
    if not interaction_or_ctx.guild:
        return
    if not await has_permission(interaction_or_ctx, "manage_guild"):
        return
    if amount < 1:
        return await interaction_or_ctx.response.send_message("Amount must be positive.", ephemeral=True)
    guild_id = interaction_or_ctx.guild.id
    await add_user_xp(guild_id, member.id, amount)
    xp = get_user_xp(guild_id, member.id)
    level, _, next_needed, _ = get_level_info(xp)
    embed = discord.Embed(title="📈 XP Added", description=f"Added **{amount} XP** to {member.mention}.\nTotal: **{xp} XP** (Lv. {level})", color=discord.Color.green())
    embed.set_footer(text="XP boost applied!")
    await (interaction_or_ctx.response.send_message(embed=embed) if hasattr(interaction_or_ctx, "response") else interaction_or_ctx.send(embed=embed))

async def xp_remove_handler(interaction_or_ctx, member: discord.Member, amount: int):
    if not interaction_or_ctx.guild:
        return
    if not await has_permission(interaction_or_ctx, "manage_guild"):
        return
    if amount < 1:
        return await interaction_or_ctx.response.send_message("Amount must be positive.", ephemeral=True)
    guild_id = interaction_or_ctx.guild.id
    current = get_user_xp(guild_id, member.id)
    new_xp = max(0, current - amount)
    set_user_xp(guild_id, member.id, new_xp)
    level, _, next_needed, _ = get_level_info(new_xp)
    embed = discord.Embed(title="📉 XP Removed", description=f"Removed **{amount} XP** from {member.mention}.\nTotal: **{new_xp} XP** (Lv. {level})", color=discord.Color.red())
    embed.set_footer(text="XP adjustment made.")
    await (interaction_or_ctx.response.send_message(embed=embed) if hasattr(interaction_or_ctx, "response") else interaction_or_ctx.send(embed=embed))

async def level_set_handler(interaction_or_ctx, member: discord.Member, level: int):
    if not interaction_or_ctx.guild:
        return
    if not await has_permission(interaction_or_ctx, "manage_guild"):
        return
    if level < 0:
        return await interaction_or_ctx.response.send_message("Level must be non-negative.", ephemeral=True)
    guild_id = interaction_or_ctx.guild.id
    target_xp = xp_for_level(level)
    set_user_xp(guild_id, member.id, target_xp)
    embed = discord.Embed(title="⚙️ Level Set", description=f"{member.mention}'s level set to **{level}**.\nNew XP: **{target_xp}**", color=discord.Color.blue())
    embed.set_footer(text="Level adjusted!")
    await (interaction_or_ctx.response.send_message(embed=embed) if hasattr(interaction_or_ctx, "response") else interaction_or_ctx.send(embed=embed))

async def rewards_handler(interaction_or_ctx):
    if not interaction_or_ctx.guild:
        return
    desc = "\n".join(f"**Level {k}**: {v} Role 🎖️" for k, v in level_rewards.items())
    embed = discord.Embed(title="🎁 Level Rewards", description=desc or "No rewards set yet.", color=discord.Color.purple())
    embed.set_footer(text="Earn XP to unlock!")
    await (interaction_or_ctx.response.send_message(embed=embed) if hasattr(interaction_or_ctx, "response") else interaction_or_ctx.send(embed=embed))

async def levelchannelset_handler(interaction_or_ctx, channel: discord.TextChannel):
    if not interaction_or_ctx.guild:
        return
    if not await has_permission(interaction_or_ctx, "manage_guild"):
        return
    level_channels[interaction_or_ctx.guild.id] = channel.id
    await save_settings()
    embed = discord.Embed(title="📢 Level Channel Set", description=f"Level notifications will now go to {channel.mention}.", color=discord.Color.green())
    embed.set_footer(text="Channel updated!")
    await (interaction_or_ctx.response.send_message(embed=embed) if hasattr(interaction_or_ctx, "response") else interaction_or_ctx.send(embed=embed))

async def meme_handler(interaction_or_ctx, keywords: Optional[str] = None):
    if hasattr(interaction_or_ctx, "response"):
        await interaction_or_ctx.response.defer()
        send_func = interaction_or_ctx.followup.send
    else:
        send_func = interaction_or_ctx.send
    url = f"https://meme-api.com/gimme/{keywords.replace(' ', '')}" if keywords else "https://meme-api.com/gimme"
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url) as resp:
                if resp.status != 200:
                    raise ValueError(f"API returned {resp.status}")
                data = await resp.json()
        except Exception as e:
            logger.error(f"Failed to fetch meme: {e}")
            embed = discord.Embed(title="❌ Error", description="Failed to fetch meme. Try again later!", color=discord.Color.red())
            return await send_func(embed=embed)
    embed = discord.Embed(title=data["title"], color=discord.Color.orange(), timestamp=datetime.now(timezone.utc))
    embed.set_footer(text="Meme from r/" + data["subreddit"] if "subreddit" in data else "Random meme")
    if data["url"].endswith(('.jpg', '.png', '.gif', '.webp')):
        embed.set_image(url=data["url"])
    else:
        embed.add_field(name="Post Link", value=data["postLink"], inline=False)
    await send_func(embed=embed)

async def coinflip_handler(interaction_or_ctx):
    result = "Heads" if random.randint(0, 1) else "Tails"
    embed = discord.Embed(title="🪙 Coin Flip", description=f"Result: **{result}**!", color=discord.Color.gold())
    embed.set_footer(text="Flip again?")
    await (interaction_or_ctx.response.send_message(embed=embed) if hasattr(interaction_or_ctx, "response") else interaction_or_ctx.send(embed=embed))

async def dice_handler(interaction_or_ctx):
    result = random.randint(1, 6)
    embed = discord.Embed(title="🎲 Dice Roll", description=f"Rolled a **{result}**!", color=discord.Color.red())
    embed.set_footer(text="Roll again?")
    await (interaction_or_ctx.response.send_message(embed=embed) if hasattr(interaction_or_ctx, "response") else interaction_or_ctx.send(embed=embed))

async def showlm_handler(interaction_or_ctx, number: int = 1):
    if not interaction_or_ctx.guild:
        return
    guild_id = interaction_or_ctx.guild.id
    await load_last_deleted_photo(guild_id)
    photos = last_deleted_photo.get(guild_id, [])
    if not photos or number < 1 or number > len(photos):
        embed = discord.Embed(title="🗑️ Deleted Photos", description="No deleted photos or invalid number.", color=discord.Color.red())
        return await (interaction_or_ctx.response.send_message(embed=embed, ephemeral=True) if hasattr(interaction_or_ctx, "response") else interaction_or_ctx.send(embed=embed, delete_after=8))
    data = photos[number - 1]
    embed = discord.Embed(title=f"🗑️ Deleted Photo #{number}", description=data["content"], color=discord.Color.red(), timestamp=datetime.fromisoformat(data["timestamp"]))
    embed.add_field(name="Author", value=data["author"], inline=True)
    embed.set_image(url=data["image_url"])
    embed.set_footer(text="Recovered from deleted message.")
    await (interaction_or_ctx.response.send_message(embed=embed) if hasattr(interaction_or_ctx, "response") else interaction_or_ctx.send(embed=embed))

async def afk_handler(interaction_or_ctx, reason: str = "AFK"):
    user = getattr(interaction_or_ctx, "user", interaction_or_ctx.author)
    afk_cache[user.id] = {"reason": reason, "since": datetime.now(timezone.utc).isoformat()}
    await save_afk()
    embed = discord.Embed(title="😴 AFK Set", description=f"Reason: **{reason}**\n\nWe'll let others know!", color=discord.Color.blue())
    embed.set_footer(text="Type any message to return.")
    await (interaction_or_ctx.response.send_message(embed=embed) if hasattr(interaction_or_ctx, "response") else interaction_or_ctx.send(embed=embed))

async def kick_handler(interaction_or_ctx, member: discord.Member, reason: Optional[str]):
    if not interaction_or_ctx.guild:
        return
    user = getattr(interaction_or_ctx, "user", interaction_or_ctx.author)
    if not await has_permission(interaction_or_ctx, "kick_members"):
        return
    guild_id = interaction_or_ctx.guild.id
    await load_mod_stats(guild_id)
    try:
        await member.kick(reason=reason)
        update_mod_stats(guild_id, user.id, "kicked")
        await send_dm(member, "kicked", user, reason)
        embed = await mod_action_embed(member, "kick", reason, user)
        await (interaction_or_ctx.response.send_message(embed=embed) if hasattr(interaction_or_ctx, "response") else interaction_or_ctx.send(embed=embed))
    except discord.Forbidden:
        embed = discord.Embed(title="❌ Error", description="Bot lacks permission to kick this member.", color=discord.Color.red())
        await (interaction_or_ctx.response.send_message(embed=embed, ephemeral=True) if hasattr(interaction_or_ctx, "response") else interaction_or_ctx.send(embed=embed, delete_after=10))

async def ban_handler(interaction_or_ctx, member: discord.Member, reason: Optional[str]):
    if not interaction_or_ctx.guild:
        return
    user = getattr(interaction_or_ctx, "user", interaction_or_ctx.author)
    if not await has_permission(interaction_or_ctx, "ban_members"):
        return
    guild_id = interaction_or_ctx.guild.id
    await load_mod_stats(guild_id)
    try:
        await member.ban(reason=reason)
        update_mod_stats(guild_id, user.id, "banned")
        await send_dm(member, "banned", user, reason)
        embed = await mod_action_embed(member, "ban", reason, user)
        await (interaction_or_ctx.response.send_message(embed=embed) if hasattr(interaction_or_ctx, "response") else interaction_or_ctx.send(embed=embed))
    except discord.Forbidden:
        embed = discord.Embed(title="❌ Error", description="Bot lacks permission to ban this member.", color=discord.Color.red())
        await (interaction_or_ctx.response.send_message(embed=embed, ephemeral=True) if hasattr(interaction_or_ctx, "response") else interaction_or_ctx.send(embed=embed, delete_after=10))

async def unban_handler(interaction_or_ctx, user: discord.User, reason: Optional[str]):
    if not interaction_or_ctx.guild:
        return
    mod = getattr(interaction_or_ctx, "user", interaction_or_ctx.author)
    if not await has_permission(interaction_or_ctx, "ban_members"):
        return
    guild_id = interaction_or_ctx.guild.id
    await load_mod_stats(guild_id)
    try:
        await interaction_or_ctx.guild.unban(user, reason=reason)
        update_mod_stats(guild_id, mod.id, "unbanned")
        await send_dm(user, "unbanned", mod, reason)
        embed = await mod_action_embed(user, "unban", reason, mod)
        await (interaction_or_ctx.response.send_message(embed=embed) if hasattr(interaction_or_ctx, "response") else interaction_or_ctx.send(embed=embed))
    except discord.Forbidden:
        embed = discord.Embed(title="❌ Error", description="Bot lacks permission to unban this user.", color=discord.Color.red())
        await (interaction_or_ctx.response.send_message(embed=embed, ephemeral=True) if hasattr(interaction_or_ctx, "response") else interaction_or_ctx.send(embed=embed, delete_after=10))

async def warn_handler(interaction_or_ctx, member: discord.Member, reason: Optional[str]):
    if not interaction_or_ctx.guild:
        return
    mod = getattr(interaction_or_ctx, "user", interaction_or_ctx.author)
    if not await has_permission(interaction_or_ctx, "kick_members"):
        return
    guild_id = interaction_or_ctx.guild.id
    await load_mod_stats(guild_id)
    update_mod_stats(guild_id, mod.id, "warned")
    await send_dm(member, "warned", mod, reason)
    embed = await mod_action_embed(member, "warn", reason, mod)
    await (interaction_or_ctx.response.send_message(embed=embed) if hasattr(interaction_or_ctx, "response") else interaction_or_ctx.send(embed=embed))

async def timeout_handler(interaction_or_ctx, member: discord.Member, duration: int, reason: Optional[str]):
    if not interaction_or_ctx.guild:
        return
    mod = getattr(interaction_or_ctx, "user", interaction_or_ctx.author)
    if not await has_permission(interaction_or_ctx, "moderate_members"):
        return
    if duration <= 0 or duration > 40320:
        return await interaction_or_ctx.response.send_message("Duration must be 1-40320 minutes.", ephemeral=True)
    guild_id = interaction_or_ctx.guild.id
    await load_mod_stats(guild_id)
    try:
        timeout_until = datetime.now(timezone.utc) + timedelta(minutes=duration)
        await member.timeout(timeout_until, reason=reason)
        update_mod_stats(guild_id, mod.id, "timed_out")
        await send_dm(member, f"timed out for {duration} minutes", mod, reason)
        embed = await mod_action_embed(member, f"timeout ({duration} min)", reason, mod)
        await (interaction_or_ctx.response.send_message(embed=embed) if hasattr(interaction_or_ctx, "response") else interaction_or_ctx.send(embed=embed))
    except discord.Forbidden:
        embed = discord.Embed(title="❌ Error", description="Bot lacks permission to timeout this member.", color=discord.Color.red())
        await (interaction_or_ctx.response.send_message(embed=embed, ephemeral=True) if hasattr(interaction_or_ctx, "response") else interaction_or_ctx.send(embed=embed, delete_after=10))

async def untimeout_handler(interaction_or_ctx, member: discord.Member, reason: Optional[str]):
    if not interaction_or_ctx.guild:
        return
    mod = getattr(interaction_or_ctx, "user", interaction_or_ctx.author)
    if not await has_permission(interaction_or_ctx, "moderate_members"):
        return
    guild_id = interaction_or_ctx.guild.id
    await load_mod_stats(guild_id)
    try:
        await member.timeout(None, reason=reason)
        update_mod_stats(guild_id, mod.id, "untimed_out")
        await send_dm(member, "timeout removed", mod, reason)
        embed = await mod_action_embed(member, "timeout removed", reason, mod)
        await (interaction_or_ctx.response.send_message(embed=embed) if hasattr(interaction_or_ctx, "response") else interaction_or_ctx.send(embed=embed))
    except discord.Forbidden:
        embed = discord.Embed(title="❌ Error", description="Bot lacks permission to remove timeout.", color=discord.Color.red())
        await (interaction_or_ctx.response.send_message(embed=embed, ephemeral=True) if hasattr(interaction_or_ctx, "response") else interaction_or_ctx.send(embed=embed, delete_after=10))

async def jail_handler(interaction_or_ctx, member: discord.Member, reason: Optional[str]):
    if not interaction_or_ctx.guild:
        return
    mod = getattr(interaction_or_ctx, "user", interaction_or_ctx.author)
    if not await has_permission(interaction_or_ctx, "manage_roles"):
        return
    guild_id = interaction_or_ctx.guild.id
    await load_mod_stats(guild_id)
    try:
        jailed_role = await get_jailed_role(interaction_or_ctx.guild)
        await member.add_roles(jailed_role, reason=reason)
        update_mod_stats(guild_id, mod.id, "jailed")
        await send_dm(member, "jailed", mod, reason)
        embed = await mod_action_embed(member, "jailed", reason, mod)
        await (interaction_or_ctx.response.send_message(embed=embed) if hasattr(interaction_or_ctx, "response") else interaction_or_ctx.send(embed=embed))
    except discord.Forbidden:
        embed = discord.Embed(title="❌ Error", description="Bot lacks permission to jail this member.", color=discord.Color.red())
        await (interaction_or_ctx.response.send_message(embed=embed, ephemeral=True) if hasattr(interaction_or_ctx, "response") else interaction_or_ctx.send(embed=embed, delete_after=10))

async def unjail_handler(interaction_or_ctx, member: discord.Member, reason: Optional[str]):
    if not interaction_or_ctx.guild:
        return
    mod = getattr(interaction_or_ctx, "user", interaction_or_ctx.author)
    if not await has_permission(interaction_or_ctx, "manage_roles"):
        return
    guild_id = interaction_or_ctx.guild.id
    await load_mod_stats(guild_id)
    try:
        jailed_role = await get_jailed_role(interaction_or_ctx.guild)
        await member.remove_roles(jailed_role, reason=reason)
        update_mod_stats(guild_id, mod.id, "unjailed")
        await send_dm(member, "unjailed", mod, reason)
        embed = await mod_action_embed(member, "unjailed", reason, mod)
        await (interaction_or_ctx.response.send_message(embed=embed) if hasattr(interaction_or_ctx, "response") else interaction_or_ctx.send(embed=embed))
    except discord.Forbidden:
        embed = discord.Embed(title="❌ Error", description="Bot lacks permission to unjail this member.", color=discord.Color.red())
        await (interaction_or_ctx.response.send_message(embed=embed, ephemeral=True) if hasattr(interaction_or_ctx, "response") else interaction_or_ctx.send(embed=embed, delete_after=10))

async def inrole_handler(interaction_or_ctx, role: Optional[discord.Role]):
    if not interaction_or_ctx.guild:
        return
    target = role or getattr(interaction_or_ctx, "user", interaction_or_ctx.author).top_role
    members = target.members[:25]
    desc = "\n".join(f"{m.mention} ({m.status})" for m in members) or "No members."
    embed = discord.Embed(title=f"👥 Members in {target.name}", description=desc, color=target.color or discord.Color.blue(), timestamp=datetime.now(timezone.utc))
    if len(target.members) > 25:
        embed.add_field(name="More", value=f"+{len(target.members) - 25} more", inline=False)
    embed.set_footer(text="Showing up to 25 members.")
    await (interaction_or_ctx.response.send_message(embed=embed) if hasattr(interaction_or_ctx, "response") else interaction_or_ctx.send(embed=embed))

async def userinfo_handler(interaction_or_ctx, member: Optional[discord.Member]):
    if not interaction_or_ctx.guild:
        return
    target = member or getattr(interaction_or_ctx, "user", interaction_or_ctx.author)
    embed = discord.Embed(title=f"👤 Profile: {target}", color=target.color or discord.Color.blue(), timestamp=datetime.now(timezone.utc))
    embed.set_thumbnail(url=target.display_avatar.url)
    embed.add_field(name="ID", value=target.id, inline=True)
    embed.add_field(name="Status", value=str(target.status).title(), inline=True)
    embed.add_field(name="Joined", value=target.joined_at.strftime("%Y-%m-%d") if target.joined_at else "N/A", inline=True)
    roles = [r.mention for r in target.roles if r != interaction_or_ctx.guild.default_role]
    embed.add_field(name="Roles", value=", ".join(roles) or "None", inline=False)
    embed.set_footer(text="User info retrieved.")
    await (interaction_or_ctx.response.send_message(embed=embed) if hasattr(interaction_or_ctx, "response") else interaction_or_ctx.send(embed=embed))

async def serverinfo_handler(interaction_or_ctx):
    if not interaction_or_ctx.guild:
        return
    guild = interaction_or_ctx.guild
    embed = discord.Embed(title=f"🖥️ Server: {guild.name}", color=discord.Color.blue(), timestamp=datetime.now(timezone.utc))
    embed.set_thumbnail(url=guild.icon.url if guild.icon else None)
    embed.add_field(name="Members", value=guild.member_count, inline=True)
    embed.add_field(name="Text Channels", value=len(guild.text_channels), inline=True)
    embed.add_field(name="Voice Channels", value=len(guild.voice_channels), inline=True)
    embed.set_footer(text="Server stats at a glance.")
    await (interaction_or_ctx.response.send_message(embed=embed) if hasattr(interaction_or_ctx, "response") else interaction_or_ctx.send(embed=embed))

async def avatar_handler(interaction_or_ctx, member: Optional[discord.Member]):
    target = member or getattr(interaction_or_ctx, "user", interaction_or_ctx.author)
    embed = discord.Embed(title=f"🖼️ {target}'s Avatar", color=discord.Color.blue(), timestamp=datetime.now(timezone.utc))
    embed.set_image(url=target.display_avatar.url)
    embed.set_footer(text="Avatar displayed.")
    await (interaction_or_ctx.response.send_message(embed=embed) if hasattr(interaction_or_ctx, "response") else interaction_or_ctx.send(embed=embed))

async def banner_handler(interaction_or_ctx, member: Optional[discord.Member]):
    target = member or getattr(interaction_or_ctx, "user", interaction_or_ctx.author)
    user_obj = await bot.fetch_user(target.id)
    if user_obj.banner:
        embed = discord.Embed(title=f"📸 {user_obj}'s Banner", color=discord.Color.blue(), timestamp=datetime.now(timezone.utc))
        embed.set_image(url=user_obj.banner.url)
        embed.set_footer(text="Banner displayed.")
        await (interaction_or_ctx.response.send_message(embed=embed) if hasattr(interaction_or_ctx, "response") else interaction_or_ctx.send(embed=embed))
    else:
        embed = discord.Embed(title="📸 No Banner", description=f"{user_obj} has no banner set.", color=discord.Color.orange())
        await (interaction_or_ctx.response.send_message(embed=embed, ephemeral=True) if hasattr(interaction_or_ctx, "response") else interaction_or_ctx.send(embed=embed, delete_after=8))

async def quote_handler(interaction_or_ctx, text: str, member: Optional[discord.Member]):
    user = getattr(interaction_or_ctx, "user", interaction_or_ctx.author)
    target = member or user
    if hasattr(interaction_or_ctx, "response"):
        await interaction_or_ctx.response.defer()
        send_func = interaction_or_ctx.followup.send
    else:
        send_func = interaction_or_ctx.send
    avatar_bytes = await _download_image_bytes(str(target.display_avatar.url))
    if not avatar_bytes:
        embed = discord.Embed(title="❌ Error", description="Failed to download avatar.", color=discord.Color.red())
        return await send_func(embed=embed)
    canvas = Image.new("RGB", (800, 400), (20, 20, 25))
    draw = ImageDraw.Draw(canvas)
    try:
        font = ImageFont.truetype("arial.ttf", 30)
    except:
        logger.warning("Arial font not found, using default")
        font = ImageFont.load_default()
    lines = _wrap_text(text, font, 760)
    for i, line in enumerate(lines):
        draw.text((20, 20 + i * 40), line, font=font, fill=(240, 240, 245))
    draw.text((20, 20 + len(lines) * 40), f"— {target.display_name}", font=font, fill=(100, 149, 237))
    buffer = BytesIO()
    canvas.save(buffer, format="JPEG", quality=95)
    buffer.seek(0)
    file = discord.File(fp=buffer, filename="quote.jpg")
    embed = discord.Embed(title="💬 Quote", description=f"By {target.display_name}", color=discord.Color.blue())
    embed.set_image(url="attachment://quote.jpg")
    embed.set_footer(text="Quote generated!")
    await send_func(embed=embed, file=file)
    buffer.close()

async def modstats_handler(interaction_or_ctx, user: Optional[discord.Member]):
    if not interaction_or_ctx.guild:
        return
    guild_id = interaction_or_ctx.guild.id
    await load_mod_stats(guild_id)
    target = user or getattr(interaction_or_ctx, "user", interaction_or_ctx.author)
    mod_stats_user = mod_stats.get(guild_id, {}).get(target.id, {"commands": [], "warned": [], "kicked": [], "banned": [], "unbanned": [], "timed_out": [], "untimed_out": [], "jailed": [], "unjailed": []})
    desc = "\n".join(f"**{action.title()}**: {len(timestamps)}" for action, timestamps in mod_stats_user.items())
    embed = discord.Embed(title=f"📊 {target}'s Mod Stats", description=desc or "No stats yet.", color=discord.Color.orange(), timestamp=datetime.now(timezone.utc))
    embed.set_footer(text="Moderation stats overview.")
    await (interaction_or_ctx.response.send_message(embed=embed) if hasattr(interaction_or_ctx, "response") else interaction_or_ctx.send(embed=embed))

async def me_handler(interaction_or_ctx):
    if not interaction_or_ctx.guild:
        return
    user = getattr(interaction_or_ctx, "user", interaction_or_ctx.author)
    guild_id = interaction_or_ctx.guild.id
    xp = get_user_xp(guild_id, user.id)
    level, xp_in_level, next_needed, progress = get_level_info(xp)
    embed = discord.Embed(title=f"📝 {user.display_name}'s Profile", color=discord.Color.blue(), timestamp=datetime.now(timezone.utc))
    embed.add_field(name="Level", value=f"**{level}** 🎖️", inline=True)
    embed.add_field(name="XP", value=f"**{xp_in_level} / {xp_in_level + next_needed}** 📊", inline=True)
    embed.add_field(name="Progress", value=f"{progress_bar(progress)} **{progress:.1f}%** 🚀", inline=False)
    embed.set_thumbnail(url=user.display_avatar.url)
    embed.set_footer(text="Your profile at a glance!")
    await (interaction_or_ctx.response.send_message(embed=embed) if hasattr(interaction_or_ctx, "response") else interaction_or_ctx.send(embed=embed))

# Add this near other bot.command definitions (around line 900 in main.py)
@bot.command(name="sync")
@commands.has_permissions(administrator=True)
async def sync_prefix(ctx):
    try:
        bot.tree.clear_commands(guild=None)
        if GUILD_ID:
            guild = discord.Object(id=GUILD_ID)
            bot.tree.copy_global_to(guild=guild)
            await bot.tree.sync(guild=guild)
            logger.info(f"Synced commands for guild {GUILD_ID}")
        await bot.tree.sync()
        logger.info("Synced global slash commands")
        await ctx.send("Slash commands synced!", delete_after=10)
    except Exception as e:
        logger.error(f"Sync failed: {e}")
        await ctx.send(f"Sync failed: {e}", delete_after=10)

# Near the getprefix_prefix command (around line 780)
@bot.command(name="prefix", aliases=["getprefix"])
@commands.cooldown(1, 30.0, commands.BucketType.guild)
async def prefix_prefix(ctx):
    await getprefix_handler(ctx)

# Events
@bot.event
async def on_ready():
    await download_db()
    await init_db()
    logger.info(f"Logged in as {bot.user}")
    await load_afk()
    await load_settings()
    for guild in bot.guilds:
        await load_mod_stats(guild.id)
        await load_xp(guild.id)
        await load_last_deleted_photo(guild.id)
        last_seen[guild.id] = datetime.now(timezone.utc).isoformat()
    await save_last_seen()
    bot.loop.create_task(periodic_db_upload())
    for attempt in range(3):
        try:
            bot.tree.clear_commands(guild=None)
            if GUILD_ID:
                guild = discord.Object(id=GUILD_ID)
                bot.tree.copy_global_to(guild=guild)
                await bot.tree.sync(guild=guild)
                logger.info(f"Synced commands for guild {GUILD_ID}")
            await bot.tree.sync()
            logger.info("Synced global slash commands")
            break
        except Exception as e:
            logger.error(f"Sync attempt {attempt + 1} failed: {e}")
            await asyncio.sleep(5)

@bot.event
async def on_guild_join(guild: discord.Guild):
    logger.info(f"Joined guild: {guild.name} ({guild.id})")
    await load_mod_stats(guild.id)
    await load_xp(guild.id)
    await load_last_deleted_photo(guild.id)
    if not GUILD_ID:
        from dotenv import set_key
        set_key(".env", "GUILD_ID", str(guild.id))

@bot.event
async def on_message_delete(message: discord.Message):
    if message.guild and message.attachments and any(att.filename.lower().endswith(('.png', '.jpg', '.jpeg', '.gif', '.webp')) for att in message.attachments):
        guild_id = message.guild.id
        photos = last_deleted_photo.setdefault(guild_id, [])
        photos.insert(0, {"author": str(message.author), "content": message.content, "image_url": message.attachments[0].url, "timestamp": datetime.now(timezone.utc).isoformat()})
        if len(photos) > 10:
            photos.pop()
        last_deleted_photo[guild_id] = photos
        await save_last_deleted_photo(guild_id)
    await bot.process_commands(message)

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return
    if message.author.id in afk_cache:
        info = afk_cache.pop(message.author.id)
        afk_time = datetime.now(timezone.utc) - datetime.fromisoformat(info['since'])
        embed = discord.Embed(title="👋 Welcome Back!", description=f"You were AFK for {str(afk_time).split('.')[0]}: {info['reason']}", color=discord.Color.green())
        await message.channel.send(f"{message.author.mention}", embed=embed, delete_after=10)
        await save_afk()
    for user in message.mentions:
        if user.id in afk_cache:
            info = afk_cache[user.id]
            embed = discord.Embed(title=f"😴 {user.display_name} is AFK", description=f"Reason: {info['reason']}\nSince: {datetime.fromisoformat(info['since']).strftime('%Y-%m-%d %H:%M')}", color=discord.Color.orange())
            await message.channel.send(embed=embed, delete_after=8)
    if message.guild:
        now = datetime.now().timestamp()
        guild_cd = msg_cooldown.setdefault(message.guild.id, {})
        if now - guild_cd.get(message.author.id, 0) > 120:
            await add_user_xp(message.guild.id, message.author.id, random.randint(15, 25))
            guild_cd[message.author.id] = now
    await bot.process_commands(message)

# Error handling
@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandOnCooldown):
        embed = discord.Embed(title="⏳ On Cooldown", description=f"Retry after {error.retry_after:.2f}s.", color=discord.Color.orange())
    elif isinstance(error, commands.MissingPermissions):
        embed = discord.Embed(title="❌ Access Denied", description="Insufficient permissions.", color=discord.Color.red())
    else:
        embed = discord.Embed(title="❌ Error", description="Something went wrong.", color=discord.Color.red())
        logger.error(f"Command error: {error}")
    await ctx.send(embed=embed, delete_after=10)

@tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CommandOnCooldown):
        embed = discord.Embed(title="⏳ On Cooldown", description=f"Retry after {error.retry_after:.2f}s.", color=discord.Color.orange())
    elif isinstance(error, app_commands.MissingPermissions):
        embed = discord.Embed(title="❌ Access Denied", description="Insufficient permissions.", color=discord.Color.red())
    else:
        embed = discord.Embed(title="❌ Error", description="Something went wrong.", color=discord.Color.red())
        logger.error(f"Slash command error: {error}")
    await interaction.response.send_message(embed=embed, ephemeral=True)

# Run bot
if __name__ == "__main__":
    asyncio.run(bot.start(TOKEN))