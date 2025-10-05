import discord
from discord import app_commands
import aiosqlite
import os
import json
from datetime import datetime, timedelta
from dotenv import load_dotenv
from flask import Flask
import threading
import os

app = Flask(__name__)

@app.route('/')
@app.route('/health')
def health():
    return 'Bot is alive! 🌿', 200

def run_flask():
    port = int(os.environ.get('PORT', 8080))  # Render sets PORT
    app.run(host='0.0.0.0', port=port, debug=False)

# Start Flask in a background thread (bot runs normally)
flask_thread = threading.Thread(target=run_flask, daemon=True)
flask_thread.start()

# Load environment variables
load_dotenv()
TOKEN = os.getenv('DISCORD_TOKEN')
GUILD_ID = 1423042923930452140  # Your guild ID as integer

# Load JSON configuration
CONFIG_FILE = 'config.json'
DEFAULT_CONFIG = {
    "welcome_channel": "welcome",
    "embed_color": 0x00ff9f,  # Linux Mint green
    "thumbnail_url": "https://www.linuxmint.com/pictures/logo.png",
    "footer_text": "Powered by MintBot"
}

if not os.path.exists(CONFIG_FILE):
    with open(CONFIG_FILE, 'w') as f:
        json.dump(DEFAULT_CONFIG, f, indent=4)

with open(CONFIG_FILE, 'r') as f:
    config = json.load(f)

# Initialize bot
intents = discord.Intents.default()
intents.guilds = True
intents.messages = True
intents.message_content = True
intents.members = True
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

# Initialize SQLite database
async def init_db():
    async with aiosqlite.connect('bot.db') as db:
        await db.execute('''
            CREATE TABLE IF NOT EXISTS infractions (
                user_id TEXT,
                guild_id TEXT,
                type TEXT,
                reason TEXT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        await db.commit()

# Register commands
@client.event
async def on_ready():
    await init_db()
    print(f'Logged in as {client.user}!')
    try:
        guild = discord.Object(id=GUILD_ID)
        tree.clear_commands(guild=None)  # Clear global commands
        synced = await tree.sync(guild=guild)
        print(f'Synced {len(synced)} command(s) to guild {GUILD_ID}')
    except Exception as e:
        print(f'Failed to sync commands: {e}')

# Welcome message
@client.event
async def on_member_join(member):
    channel = discord.utils.get(member.guild.text_channels, name=config['welcome_channel'])
    if channel:
        embed = discord.Embed(
            title=f"Welcome, {member.name}! 🌿",
            description="Enjoy your stay in our server! 🎉 Get started by checking the rules and introducing yourself.",
            color=config['embed_color']
        )
        embed.set_thumbnail(url=config['thumbnail_url'])
        embed.set_footer(text=config['footer_text'], icon_url=config['thumbnail_url'])
        embed.add_field(name="Joined", value=member.joined_at.strftime('%Y-%m-%d %H:%M:%S'), inline=True)
        embed.add_field(name="User ID", value=str(member.id), inline=True)
        await channel.send(embed=embed)

# Helper: Check permissions
def has_permission(interaction: discord.Interaction, permission: discord.Permissions):
    if not interaction.user.guild_permissions >= permission:
        embed = discord.Embed(
            title="Permission Denied",
            description="You lack the required permissions to use this command!",
            color=discord.Color.red()
        )
        embed.set_thumbnail(url=config['thumbnail_url'])
        embed.set_footer(text=config['footer_text'], icon_url=config['thumbnail_url'])
        return embed
    return None

# Helper: Log infraction
async def log_infraction(user_id: str, guild_id: str, infraction_type: str, reason: str):
    async with aiosqlite.connect('bot.db') as db:
        await db.execute(
            'INSERT INTO infractions (user_id, guild_id, type, reason) VALUES (?, ?, ?, ?)',
            (str(user_id), str(guild_id), infraction_type, reason)
        )
        await db.commit()

# Helper: Parse duration
def parse_duration(duration: str) -> int:
    units = {'s': 1, 'm': 60, 'h': 3600, 'd': 86400}
    duration = duration.strip().lower()
    if not duration[:-1].isdigit():
        raise ValueError('Invalid duration format (e.g., 10m, 1h)')
    value = int(duration[:-1])
    unit = duration[-1]
    if unit not in units:
        raise ValueError('Invalid unit (use s, m, h, or d)')
    return value * units[unit]

# Confirmation view for dangerous commands
class ConfirmationView(discord.ui.View):
    def __init__(self, action_func, action_name: str, target: str, details: str):
        super().__init__(timeout=60)
        self.action_func = action_func
        self.action_name = action_name
        self.target = target
        self.details = details

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.green)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.action_func()
        embed = discord.Embed(
            title=f"{self.action_name} Confirmed",
            description=f"Action executed: {self.details}",
            color=config['embed_color']
        )
        embed.set_thumbnail(url=config['thumbnail_url'])
        embed.set_footer(text=config['footer_text'], icon_url=config['thumbnail_url'])
        await interaction.response.edit_message(embed=embed, view=None)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.red)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = discord.Embed(
            title=f"{self.action_name} Cancelled",
            description="Action was cancelled.",
            color=discord.Color.red()
        )
        embed.set_thumbnail(url=config['thumbnail_url'])
        embed.set_footer(text=config['footer_text'], icon_url=config['thumbnail_url'])
        await interaction.response.edit_message(embed=embed, view=None)

# Commands
@tree.command(name='ban', description='Ban a user from the server', guild=discord.Object(id=GUILD_ID))
@app_commands.describe(target='User to ban', reason='Reason for the ban')
async def ban(interaction: discord.Interaction, target: discord.Member, reason: str = 'No reason provided'):
    if perm_error := has_permission(interaction, discord.Permissions(ban_members=True)):
        return await interaction.response.send_message(embed=perm_error, ephemeral=True)
    if not target.bannable or target.top_role >= interaction.guild.me.top_role:
        embed = discord.Embed(
            title="Ban Failed",
            description=f"Cannot ban {target} (bot lacks permission or role hierarchy issue)!",
            color=discord.Color.red()
        )
        embed.set_thumbnail(url=config['thumbnail_url'])
        embed.set_footer(text=config['footer_text'], icon_url=config['thumbnail_url'])
        return await interaction.response.send_message(embed=embed, ephemeral=True)

    async def perform_action():
        await target.ban(reason=reason)
        await log_infraction(target.id, interaction.guild.id, 'ban', reason)
        embed = discord.Embed(
            title="User Banned",
            description=f"**{target}** has been banned for: {reason}",
            color=config['embed_color']
        )
        embed.set_thumbnail(url=config['thumbnail_url'])
        embed.set_footer(text=config['footer_text'], icon_url=config['thumbnail_url'])
        embed.add_field(name="Moderator", value=interaction.user.mention, inline=True)
        embed.add_field(name="Timestamp", value=datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S'), inline=True)
        await interaction.followup.send(embed=embed)

    view = ConfirmationView(perform_action, "Ban", str(target), f"Ban {target} for: {reason}")
    embed = discord.Embed(
        title="Confirm Ban",
        description=f"Are you sure you want to ban **{target}** for: {reason}?",
        color=config['embed_color']
    )
    embed.set_thumbnail(url=config['thumbnail_url'])
    embed.set_footer(text=config['footer_text'], icon_url=config['thumbnail_url'])
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

@tree.command(name='unban', description='Unban a user from the server', guild=discord.Object(id=GUILD_ID))
@app_commands.describe(target='User to unban (use ID or mention)')
async def unban(interaction: discord.Interaction, target: discord.User):
    if perm_error := has_permission(interaction, discord.Permissions(ban_members=True)):
        return await interaction.response.send_message(embed=perm_error, ephemeral=True)
    try:
        await interaction.guild.unban(target)
        embed = discord.Embed(
            title="User Unbanned",
            description=f"**{target}** has been unbanned.",
            color=config['embed_color']
        )
        embed.set_thumbnail(url=config['thumbnail_url'])
        embed.set_footer(text=config['footer_text'], icon_url=config['thumbnail_url'])
        embed.add_field(name="Moderator", value=interaction.user.mention, inline=True)
        embed.add_field(name="Timestamp", value=datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S'), inline=True)
        await interaction.response.send_message(embed=embed)
    except discord.errors.NotFound:
        embed = discord.Embed(
            title="Unban Failed",
            description=f"**{target}** is not banned!",
            color=discord.Color.red()
        )
        embed.set_thumbnail(url=config['thumbnail_url'])
        embed.set_footer(text=config['footer_text'], icon_url=config['thumbnail_url'])
        await interaction.response.send_message(embed=embed, ephemeral=True)

@tree.command(name='kick', description='Kick a user from the server', guild=discord.Object(id=GUILD_ID))
@app_commands.describe(target='User to kick', reason='Reason for the kick')
async def kick(interaction: discord.Interaction, target: discord.Member, reason: str = 'No reason provided'):
    if perm_error := has_permission(interaction, discord.Permissions(kick_members=True)):
        return await interaction.response.send_message(embed=perm_error, ephemeral=True)
    if not target.kickable or target.top_role >= interaction.guild.me.top_role:
        embed = discord.Embed(
            title="Kick Failed",
            description=f"Cannot kick **{target}** (bot lacks permission or role hierarchy issue)!",
            color=discord.Color.red()
        )
        embed.set_thumbnail(url=config['thumbnail_url'])
        embed.set_footer(text=config['footer_text'], icon_url=config['thumbnail_url'])
        return await interaction.response.send_message(embed=embed, ephemeral=True)

    async def perform_action():
        await target.kick(reason=reason)
        await log_infraction(target.id, interaction.guild.id, 'kick', reason)
        embed = discord.Embed(
            title="User Kicked",
            description=f"**{target}** has been kicked for: {reason}",
            color=config['embed_color']
        )
        embed.set_thumbnail(url=config['thumbnail_url'])
        embed.set_footer(text=config['footer_text'], icon_url=config['thumbnail_url'])
        embed.add_field(name="Moderator", value=interaction.user.mention, inline=True)
        embed.add_field(name="Timestamp", value=datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S'), inline=True)
        await interaction.followup.send(embed=embed)

    view = ConfirmationView(perform_action, "Kick", str(target), f"Kick {target} for: {reason}")
    embed = discord.Embed(
        title="Confirm Kick",
        description=f"Are you sure you want to kick **{target}** for: {reason}?",
        color=config['embed_color']
    )
    embed.set_thumbnail(url=config['thumbnail_url'])
    embed.set_footer(text=config['footer_text'], icon_url=config['thumbnail_url'])
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

@tree.command(name='mute', description='Mute a user in the server', guild=discord.Object(id=GUILD_ID))
@app_commands.describe(target='User to mute', duration='Duration (e.g., 10m, 1h)', reason='Reason for mute')
async def mute(interaction: discord.Interaction, target: discord.Member, duration: str, reason: str = 'No reason provided'):
    if perm_error := has_permission(interaction, discord.Permissions(moderate_members=True)):
        return await interaction.response.send_message(embed=perm_error, ephemeral=True)
    if target.is_timed_out():
        embed = discord.Embed(
            title="Mute Failed",
            description=f"**{target}** is already muted!",
            color=discord.Color.red()
        )
        embed.set_thumbnail(url=config['thumbnail_url'])
        embed.set_footer(text=config['footer_text'], icon_url=config['thumbnail_url'])
        return await interaction.response.send_message(embed=embed, ephemeral=True)
    try:
        seconds = parse_duration(duration)
        until = datetime.utcnow() + timedelta(seconds=seconds)
        async def perform_action():
            await target.timeout(until, reason=reason)
            await log_infraction(target.id, interaction.guild.id, 'mute', reason)
            embed = discord.Embed(
                title="User Muted",
                description=f"**{target}** has been muted for {duration}: {reason}",
                color=config['embed_color']
            )
            embed.set_thumbnail(url=config['thumbnail_url'])
            embed.set_footer(text=config['footer_text'], icon_url=config['thumbnail_url'])
            embed.add_field(name="Moderator", value=interaction.user.mention, inline=True)
            embed.add_field(name="Timestamp", value=datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S'), inline=True)
            await interaction.followup.send(embed=embed)

        view = ConfirmationView(perform_action, "Mute", str(target), f"Mute {target} for {duration}: {reason}")
        embed = discord.Embed(
            title="Confirm Mute",
            description=f"Are you sure you want to mute **{target}** for {duration}: {reason}?",
            color=config['embed_color']
        )
        embed.set_thumbnail(url=config['thumbnail_url'])
        embed.set_footer(text=config['footer_text'], icon_url=config['thumbnail_url'])
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
    except ValueError as e:
        embed = discord.Embed(
            title="Invalid Duration",
            description=str(e),
            color=discord.Color.red()
        )
        embed.set_thumbnail(url=config['thumbnail_url'])
        embed.set_footer(text=config['footer_text'], icon_url=config['thumbnail_url'])
        await interaction.response.send_message(embed=embed, ephemeral=True)

@tree.command(name='unmute', description='Unmute a user in the server', guild=discord.Object(id=GUILD_ID))
@app_commands.describe(target='User to unmute')
async def unmute(interaction: discord.Interaction, target: discord.Member):
    if perm_error := has_permission(interaction, discord.Permissions(moderate_members=True)):
        return await interaction.response.send_message(embed=perm_error, ephemeral=True)
    if not target.is_timed_out():
        embed = discord.Embed(
            title="Unmute Failed",
            description=f"**{target}** is not muted!",
            color=discord.Color.red()
        )
        embed.set_thumbnail(url=config['thumbnail_url'])
        embed.set_footer(text=config['footer_text'], icon_url=config['thumbnail_url'])
        return await interaction.response.send_message(embed=embed, ephemeral=True)
    await target.timeout(None)
    embed = discord.Embed(
        title="User Unmuted",
        description=f"**{target}** has been unmuted.",
        color=config['embed_color']
    )
    embed.set_thumbnail(url=config['thumbnail_url'])
    embed.set_footer(text=config['footer_text'], icon_url=config['thumbnail_url'])
    embed.add_field(name="Moderator", value=interaction.user.mention, inline=True)
    embed.add_field(name="Timestamp", value=datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S'), inline=True)
    await interaction.response.send_message(embed=embed)

@tree.command(name='tempban', description='Temporarily ban a user from the server', guild=discord.Object(id=GUILD_ID))
@app_commands.describe(target='User to ban', duration='Duration (e.g., 1h, 1d)', reason='Reason for ban')
async def tempban(interaction: discord.Interaction, target: discord.Member, duration: str, reason: str = 'No reason provided'):
    if perm_error := has_permission(interaction, discord.Permissions(ban_members=True)):
        return await interaction.response.send_message(embed=perm_error, ephemeral=True)
    if not target.bannable or target.top_role >= interaction.guild.me.top_role:
        embed = discord.Embed(
            title="Tempban Failed",
            description=f"Cannot ban **{target}** (bot lacks permission or role hierarchy issue)!",
            color=discord.Color.red()
        )
        embed.set_thumbnail(url=config['thumbnail_url'])
        embed.set_footer(text=config['footer_text'], icon_url=config['thumbnail_url'])
        return await interaction.response.send_message(embed=embed, ephemeral=True)
    try:
        seconds = parse_duration(duration)
        async def perform_action():
            await target.ban(reason=reason)
            await log_infraction(target.id, interaction.guild.id, 'tempban', reason)
            embed = discord.Embed(
                title="User Temp-Banned",
                description=f"**{target}** has been temp-banned for {duration}: {reason}",
                color=config['embed_color']
            )
            embed.set_thumbnail(url=config['thumbnail_url'])
            embed.set_footer(text=config['footer_text'], icon_url=config['thumbnail_url'])
            embed.add_field(name="Moderator", value=interaction.user.mention, inline=True)
            embed.add_field(name="Timestamp", value=datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S'), inline=True)
            await interaction.followup.send(embed=embed)
            # Schedule unban
            async def unban_task():
                await interaction.guild.unban(target)
            client.loop.call_later(seconds, lambda: client.loop.create_task(unban_task()))

        view = ConfirmationView(perform_action, "Tempban", str(target), f"Temp-ban {target} for {duration}: {reason}")
        embed = discord.Embed(
            title="Confirm Tempban",
            description=f"Are you sure you want to temp-ban **{target}** for {duration}: {reason}?",
            color=config['embed_color']
        )
        embed.set_thumbnail(url=config['thumbnail_url'])
        embed.set_footer(text=config['footer_text'], icon_url=config['thumbnail_url'])
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
    except ValueError as e:
        embed = discord.Embed(
            title="Invalid Duration",
            description=str(e),
            color=discord.Color.red()
        )
        embed.set_thumbnail(url=config['thumbnail_url'])
        embed.set_footer(text=config['footer_text'], icon_url=config['thumbnail_url'])
        await interaction.response.send_message(embed=embed, ephemeral=True)

@tree.command(name='tempmute', description='Temporarily mute a user in the server', guild=discord.Object(id=GUILD_ID))
@app_commands.describe(target='User to mute', duration='Duration (e.g., 10m, 1h)', reason='Reason for mute')
async def tempmute(interaction: discord.Interaction, target: discord.Member, duration: str, reason: str = 'No reason provided'):
    if perm_error := has_permission(interaction, discord.Permissions(moderate_members=True)):
        return await interaction.response.send_message(embed=perm_error, ephemeral=True)
    if target.is_timed_out():
        embed = discord.Embed(
            title="Tempmute Failed",
            description=f"**{target}** is already muted!",
            color=discord.Color.red()
        )
        embed.set_thumbnail(url=config['thumbnail_url'])
        embed.set_footer(text=config['footer_text'], icon_url=config['thumbnail_url'])
        return await interaction.response.send_message(embed=embed, ephemeral=True)
    try:
        seconds = parse_duration(duration)
        until = datetime.utcnow() + timedelta(seconds=seconds)
        async def perform_action():
            await target.timeout(until, reason=reason)
            await log_infraction(target.id, interaction.guild.id, 'tempmute', reason)
            embed = discord.Embed(
                title="User Temp-Muted",
                description=f"**{target}** has been temp-muted for {duration}: {reason}",
                color=config['embed_color']
            )
            embed.set_thumbnail(url=config['thumbnail_url'])
            embed.set_footer(text=config['footer_text'], icon_url=config['thumbnail_url'])
            embed.add_field(name="Moderator", value=interaction.user.mention, inline=True)
            embed.add_field(name="Timestamp", value=datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S'), inline=True)
            await interaction.followup.send(embed=embed)

        view = ConfirmationView(perform_action, "Tempmute", str(target), f"Temp-mute {target} for {duration}: {reason}")
        embed = discord.Embed(
            title="Confirm Tempmute",
            description=f"Are you sure you want to temp-mute **{target}** for {duration}: {reason}?",
            color=config['embed_color']
        )
        embed.set_thumbnail(url=config['thumbnail_url'])
        embed.set_footer(text=config['footer_text'], icon_url=config['thumbnail_url'])
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
    except ValueError as e:
        embed = discord.Embed(
            title="Invalid Duration",
            description=str(e),
            color=discord.Color.red()
        )
        embed.set_thumbnail(url=config['thumbnail_url'])
        embed.set_footer(text=config['footer_text'], icon_url=config['thumbnail_url'])
        await interaction.response.send_message(embed=embed, ephemeral=True)

@tree.command(name='warn', description='Warn a user (auto-mutes after 3 warnings)', guild=discord.Object(id=GUILD_ID))
@app_commands.describe(target='User to warn', reason='Reason for the warning')
async def warn(interaction: discord.Interaction, target: discord.Member, reason: str = 'No reason provided'):
    if perm_error := has_permission(interaction, discord.Permissions(moderate_members=True)):
        return await interaction.response.send_message(embed=perm_error, ephemeral=True)
    await log_infraction(target.id, interaction.guild.id, 'warn', reason)
    async with aiosqlite.connect('bot.db') as db:
        cursor = await db.execute(
            'SELECT * FROM infractions WHERE user_id = ? AND guild_id = ? AND type = ?',
            (str(target.id), str(interaction.guild.id), 'warn')
        )
        rows = await cursor.fetchall()
        warn_count = len(rows)
        embed = discord.Embed(
            title="User Warned",
            description=f"**{target}** has been warned for: {reason} ({warn_count}/3 warnings)",
            color=config['embed_color']
        )
        embed.set_thumbnail(url=config['thumbnail_url'])
        embed.set_footer(text=config['footer_text'], icon_url=config['thumbnail_url'])
        embed.add_field(name="Moderator", value=interaction.user.mention, inline=True)
        embed.add_field(name="Timestamp", value=datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S'), inline=True)
        await interaction.response.send_message(embed=embed)
        if warn_count >= 3 and not target.is_timed_out():
            await target.timeout(datetime.utcnow() + timedelta(hours=1), reason='Auto-muted: 3 warnings')
            await log_infraction(target.id, interaction.guild.id, 'mute', 'Auto-muted: 3 warnings')
            embed = discord.Embed(
                title="User Auto-Muted",
                description=f"**{target}** has been auto-muted for 1 hour due to 3 warnings.",
                color=config['embed_color']
            )
            embed.set_thumbnail(url=config['thumbnail_url'])
            embed.set_footer(text=config['footer_text'], icon_url=config['thumbnail_url'])
            embed.add_field(name="Moderator", value=interaction.user.mention, inline=True)
            embed.add_field(name="Timestamp", value=datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S'), inline=True)
            await interaction.followup.send(embed=embed)

@tree.command(name='clear', description='Delete messages in the channel', guild=discord.Object(id=GUILD_ID))
@app_commands.describe(amount='Number of messages to delete (1-100)')
async def clear(interaction: discord.Interaction, amount: int):
    if perm_error := has_permission(interaction, discord.Permissions(manage_messages=True)):
        return await interaction.response.send_message(embed=perm_error, ephemeral=True)
    if amount < 1 or amount > 100:
        embed = discord.Embed(
            title="Invalid Amount",
            description="Amount must be between 1 and 100!",
            color=discord.Color.red()
        )
        embed.set_thumbnail(url=config['thumbnail_url'])
        embed.set_footer(text=config['footer_text'], icon_url=config['thumbnail_url'])
        return await interaction.response.send_message(embed=embed, ephemeral=True)
    await interaction.channel.purge(limit=amount)
    embed = discord.Embed(
        title="Messages Cleared",
        description=f"Deleted {amount} messages from this channel.",
        color=config['embed_color']
    )
    embed.set_thumbnail(url=config['thumbnail_url'])
    embed.set_footer(text=config['footer_text'], icon_url=config['thumbnail_url'])
    embed.add_field(name="Moderator", value=interaction.user.mention, inline=True)
    embed.add_field(name="Timestamp", value=datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S'), inline=True)
    await interaction.response.send_message(embed=embed, ephemeral=True)

@tree.command(name='user-info', description='Show information about a user', guild=discord.Object(id=GUILD_ID))
@app_commands.describe(target='User to check')
async def user_info(interaction: discord.Interaction, target: discord.Member):
    embed = discord.Embed(
        title=f"User Info: {target.name}",
        color=config['embed_color']
    )
    embed.set_thumbnail(url=target.avatar.url if target.avatar else target.default_avatar.url)
    embed.set_footer(text=config['footer_text'], icon_url=config['thumbnail_url'])
    embed.add_field(name="Joined Server", value=target.joined_at.strftime('%Y-%m-%d %H:%M:%S'), inline=True)
    embed.add_field(name="Roles", value=', '.join([r.name for r in target.roles[1:]]) or 'None', inline=True)
    embed.add_field(name="User ID", value=str(target.id), inline=True)
    embed.add_field(name="Account Created", value=target.created_at.strftime('%Y-%m-%d %H:%M:%S'), inline=True)
    await interaction.response.send_message(embed=embed)

@tree.command(name='role-info', description='Show information about a role', guild=discord.Object(id=GUILD_ID))
@app_commands.describe(role='Role to check')
async def role_info(interaction: discord.Interaction, role: discord.Role):
    embed = discord.Embed(
        title=f"Role Info: {role.name}",
        color=config['embed_color']
    )
    embed.set_thumbnail(url=config['thumbnail_url'])
    embed.set_footer(text=config['footer_text'], icon_url=config['thumbnail_url'])
    embed.add_field(name="Color", value=str(role.color), inline=True)
    embed.add_field(name="Members", value=str(len(role.members)), inline=True)
    embed.add_field(name="Position", value=str(role.position), inline=True)
    embed.add_field(name="Created", value=role.created_at.strftime('%Y-%m-%d %H:%M:%S'), inline=True)
    await interaction.response.send_message(embed=embed)

@tree.command(name='server-info', description='Show server information', guild=discord.Object(id=GUILD_ID))
async def server_info(interaction: discord.Interaction):
    guild = interaction.guild
    embed = discord.Embed(
        title=f"Server Info: {guild.name}",
        color=config['embed_color']
    )
    embed.set_thumbnail(url=guild.icon.url if guild.icon else config['thumbnail_url'])
    embed.set_footer(text=config['footer_text'], icon_url=config['thumbnail_url'])
    embed.add_field(name="Members", value=str(guild.member_count), inline=True)
    embed.add_field(name="Channels", value=str(len(guild.channels)), inline=True)
    embed.add_field(name="Created", value=guild.created_at.strftime('%Y-%m-%d %H:%M:%S'), inline=True)
    embed.add_field(name="Owner", value=guild.owner.mention, inline=True)
    await interaction.response.send_message(embed=embed)

@tree.command(name='infractions', description='View a user\'s infractions', guild=discord.Object(id=GUILD_ID))
@app_commands.describe(target='User to check')
async def infractions(interaction: discord.Interaction, target: discord.User):
    if perm_error := has_permission(interaction, discord.Permissions(moderate_members=True)):
        return await interaction.response.send_message(embed=perm_error, ephemeral=True)
    async with aiosqlite.connect('bot.db') as db:
        cursor = await db.execute(
            'SELECT type, reason, timestamp FROM infractions WHERE user_id = ? AND guild_id = ?',
            (str(target.id), str(interaction.guild.id))
        )
        rows = await cursor.fetchall()
        if not rows:
            embed = discord.Embed(
                title="No Infractions",
                description=f"No infractions found for **{target}**.",
                color=config['embed_color']
            )
            embed.set_thumbnail(url=config['thumbnail_url'])
            embed.set_footer(text=config['footer_text'], icon_url=config['thumbnail_url'])
            return await interaction.response.send_message(embed=embed)
        embed = discord.Embed(
            title=f"Infractions for {target.name}",
            description='\n'.join([f"**{r[0].upper()}** ({r[2]}): {r[1]}" for r in rows]),
            color=config['embed_color']
        )
        embed.set_thumbnail(url=config['thumbnail_url'])
        embed.set_footer(text=config['footer_text'], icon_url=config['thumbnail_url'])
        embed.add_field(name="Total Infractions", value=str(len(rows)), inline=True)
        await interaction.response.send_message(embed=embed)

@tree.command(name='clear-all-infractions', description='Clear all infractions for a user', guild=discord.Object(id=GUILD_ID))
@app_commands.describe(target='User to clear')
async def clear_all_infractions(interaction: discord.Interaction, target: discord.User):
    if perm_error := has_permission(interaction, discord.Permissions(moderate_members=True)):
        return await interaction.response.send_message(embed=perm_error, ephemeral=True)
    async with aiosqlite.connect('bot.db') as db:
        await db.execute(
            'DELETE FROM infractions WHERE user_id = ? AND guild_id = ?',
            (str(target.id), str(interaction.guild.id))
        )
        await db.commit()
    embed = discord.Embed(
        title="Infractions Cleared",
        description=f"All infractions for **{target}** have been cleared.",
        color=config['embed_color']
    )
    embed.set_thumbnail(url=config['thumbnail_url'])
    embed.set_footer(text=config['footer_text'], icon_url=config['thumbnail_url'])
    embed.add_field(name="Moderator", value=interaction.user.mention, inline=True)
    embed.add_field(name="Timestamp", value=datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S'), inline=True)
    await interaction.response.send_message(embed=embed)

@tree.command(name='slow-mode', description='Set slowmode for the channel', guild=discord.Object(id=GUILD_ID))
@app_commands.describe(duration='Duration (e.g., 10s, off)')
async def slow_mode(interaction: discord.Interaction, duration: str):
    if perm_error := has_permission(interaction, discord.Permissions(manage_channels=True)):
        return await interaction.response.send_message(embed=perm_error, ephemeral=True)
    if duration.lower() == 'off':
        seconds = 0
    else:
        try:
            seconds = parse_duration(duration)
        except ValueError as e:
            embed = discord.Embed(
                title="Invalid Duration",
                description=str(e),
                color=discord.Color.red()
            )
            embed.set_thumbnail(url=config['thumbnail_url'])
            embed.set_footer(text=config['footer_text'], icon_url=config['thumbnail_url'])
            return await interaction.response.send_message(embed=embed, ephemeral=True)
    await interaction.channel.edit(slowmode_delay=seconds)
    embed = discord.Embed(
        title="Slowmode Updated",
        description=f"Slowmode set to {duration if seconds else 'off'}.",
        color=config['embed_color']
    )
    embed.set_thumbnail(url=config['thumbnail_url'])
    embed.set_footer(text=config['footer_text'], icon_url=config['thumbnail_url'])
    embed.add_field(name="Moderator", value=interaction.user.mention, inline=True)
    embed.add_field(name="Timestamp", value=datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S'), inline=True)
    await interaction.response.send_message(embed=embed)

@tree.command(name='add', description='Add a role to a user', guild=discord.Object(id=GUILD_ID))
@app_commands.describe(target='User to add role', role='Role to add')
async def add(interaction: discord.Interaction, target: discord.Member, role: discord.Role):
    if perm_error := has_permission(interaction, discord.Permissions(manage_roles=True)):
        return await interaction.response.send_message(embed=perm_error, ephemeral=True)
    if role.position >= interaction.guild.me.top_role.position:
        embed = discord.Embed(
            title="Role Assignment Failed",
            description=f"Cannot assign **{role.name}** (role is too high in hierarchy)!",
            color=discord.Color.red()
        )
        embed.set_thumbnail(url=config['thumbnail_url'])
        embed.set_footer(text=config['footer_text'], icon_url=config['thumbnail_url'])
        return await interaction.response.send_message(embed=embed, ephemeral=True)
    await target.add_roles(role)
    embed = discord.Embed(
        title="Role Assigned",
        description=f"Added role **{role.name}** to **{target}**.",
        color=config['embed_color']
    )
    embed.set_thumbnail(url=config['thumbnail_url'])
    embed.set_footer(text=config['footer_text'], icon_url=config['thumbnail_url'])
    embed.add_field(name="Moderator", value=interaction.user.mention, inline=True)
    embed.add_field(name="Timestamp", value=datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S'), inline=True)
    await interaction.response.send_message(embed=embed)

@tree.command(name='forcename', description="Force change a user's nickname", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(target='User to rename', nickname='New nickname')
async def forcename(interaction: discord.Interaction, target: discord.Member, nickname: str):
    if perm_error := has_permission(interaction, discord.Permissions(manage_nicknames=True)):
        return await interaction.response.send_message(embed=perm_error, ephemeral=True)
    if target.top_role >= interaction.guild.me.top_role:
        embed = discord.Embed(
            title="Nickname Change Failed",
            description=f"Cannot change nickname for **{target}** (bot lacks permission or role hierarchy issue)!",
            color=discord.Color.red()
        )
        embed.set_thumbnail(url=config['thumbnail_url'])
        embed.set_footer(text=config['footer_text'], icon_url=config['thumbnail_url'])
        return await interaction.response.send_message(embed=embed, ephemeral=True)
    await target.edit(nick=nickname)
    embed = discord.Embed(
        title="Nickname Changed",
        description=f"Set **{target}**'s nickname to **{nickname}**.",
        color=config['embed_color']
    )
    embed.set_thumbnail(url=config['thumbnail_url'])
    embed.set_footer(text=config['footer_text'], icon_url=config['thumbnail_url'])
    embed.add_field(name="Moderator", value=interaction.user.mention, inline=True)
    embed.add_field(name="Timestamp", value=datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S'), inline=True)
    await interaction.response.send_message(embed=embed)

@tree.command(name='remove-forcename', description="Reset a user's nickname", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(target='User to reset')
async def remove_forcename(interaction: discord.Interaction, target: discord.Member):
    if perm_error := has_permission(interaction, discord.Permissions(manage_nicknames=True)):
        return await interaction.response.send_message(embed=perm_error, ephemeral=True)
    if target.top_role >= interaction.guild.me.top_role:
        embed = discord.Embed(
            title="Nickname Reset Failed",
            description=f"Cannot reset nickname for **{target}** (bot lacks permission or role hierarchy issue)!",
            color=discord.Color.red()
        )
        embed.set_thumbnail(url=config['thumbnail_url'])
        embed.set_footer(text=config['footer_text'], icon_url=config['thumbnail_url'])
        return await interaction.response.send_message(embed=embed, ephemeral=True)
    await target.edit(nick=None)
    embed = discord.Embed(
        title="Nickname Reset",
        description=f"Reset **{target}**'s nickname.",
        color=config['embed_color']
    )
    embed.set_thumbnail(url=config['thumbnail_url'])
    embed.set_footer(text=config['footer_text'], icon_url=config['thumbnail_url'])
    embed.add_field(name="Moderator", value=interaction.user.mention, inline=True)
    embed.add_field(name="Timestamp", value=datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S'), inline=True)
    await interaction.response.send_message(embed=embed)

# Run bot
client.run(TOKEN)