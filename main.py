import os
import signal
import asyncio
import logging
import aiohttp
from aiohttp import web
import discord
from discord.ext import commands
from discord import ui
from dotenv import load_dotenv

load_dotenv()

# Set up logging for Railway deployment logs
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("TicketBot")

TOKEN = os.getenv("DISCORD_TOKEN")
STAFF_ROLE_ID = int(os.getenv("STAFF_ROLE_ID", 0))
PORT = int(os.getenv("PORT", 8080))

intents = discord.Intents.default()
intents.guilds = True
intents.messages = True
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)


def is_staff(member: discord.Member) -> bool:
    if member.guild_permissions.administrator or member.guild_permissions.manage_channels:
        return True
    if STAFF_ROLE_ID and any(role.id == STAFF_ROLE_ID for role in member.roles):
        return True
    return False


def parse_topic_metadata(topic: str | None) -> dict:
    data = {"owner_id": None, "type": "General Help", "claimed_by": None}
    if not topic:
        return data
    for item in topic.split("|"):
        if ":" in item:
            key, val = item.split(":", 1)
            key = key.strip()
            val = val.strip()
            if key == "Ticket Owner ID":
                try:
                    data["owner_id"] = int(val)
                except ValueError:
                    pass
            elif key == "Type":
                data["type"] = val
            elif key == "Claimed By":
                try:
                    data["claimed_by"] = int(val) if val.lower() != "none" else None
                except ValueError:
                    pass
    return data


# ---------------- Modal: Collect ticket reason ----------------
class TicketReasonModal(ui.Modal):
    def __init__(self, category_name: str):
        super().__init__(title=f"{category_name[:30]} Ticket")
        self.category_name = category_name

        self.subject = ui.TextInput(
            label="Subject",
            placeholder="Brief summary of your inquiry",
            max_length=100,
            required=True
        )
        self.details = ui.TextInput(
            label="Description",
            style=discord.TextStyle.paragraph,
            placeholder="Provide all relevant details here...",
            max_length=1000,
            required=True
        )
        self.add_item(self.subject)
        self.add_item(self.details)

    async def on_submit(self, interaction: discord.Interaction):
        guild = interaction.guild
        user = interaction.user

        if not guild or not isinstance(user, discord.Member):
            await interaction.response.send_message("Unable to process request outside of a server.", ephemeral=True)
            return

        expected_suffix = f"-{user.id % 10000:04d}"
        for ch in guild.text_channels:
            if ch.name.startswith("ticket-") and ch.name.endswith(expected_suffix):
                await interaction.response.send_message(
                    f"You already have an open ticket: {ch.mention}",
                    ephemeral=True
                )
                return

        category = discord.utils.get(guild.categories, name="Tickets")
        if not category:
            category = await guild.create_category("Tickets")

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            user: discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
                attach_files=True
            ),
            guild.me: discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                manage_channels=True,
                manage_permissions=True
            )
        }

        if STAFF_ROLE_ID:
            staff_role = guild.get_role(STAFF_ROLE_ID)
            if staff_role:
                overwrites[staff_role] = discord.PermissionOverwrite(
                    view_channel=True,
                    send_messages=True,
                    read_message_history=True,
                    manage_messages=True
                )

        clean_username = "".join(c for c in user.name.lower() if c.isalnum())[:12]
        channel_name = f"ticket-{clean_username}-{user.id % 10000:04d}"

        ticket_channel = await guild.create_text_channel(
            name=channel_name,
            category=category,
            overwrites=overwrites,
            topic=f"Ticket Owner ID: {user.id} | Type: {self.category_name} | Claimed By: None"
        )

        embed = discord.Embed(
            title=f"Support Ticket — {self.category_name}",
            description=f"Welcome {user.mention}! Support staff will be with you shortly.\nUse the options below to manage this ticket.",
            color=discord.Color.blurple()
        )
        embed.add_field(name="Subject", value=self.subject.value, inline=False)
        embed.add_field(name="Description", value=self.details.value, inline=False)
        embed.add_field(name="Claim Status", value="Unclaimed", inline=False)
        embed.set_footer(text=f"User ID: {user.id}")

        await ticket_channel.send(content=f"{user.mention}", embed=embed, view=TicketControlView(is_claimed=False))
        await interaction.response.send_message(
            f"Your ticket has been created: {ticket_channel.mention}",
            ephemeral=True
        )


# ---------------- Select Menu: Dropdown categories ----------------
class TicketCategorySelect(ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label="General Help", description="General inquiries or questions", emoji="❓", value="General Help"),
            discord.SelectOption(label="Orders & Store", description="Store items, orders, or services", emoji="📦", value="Orders & Store"),
            discord.SelectOption(label="Report Issue", description="Report bugs or server issues", emoji="⚠️", value="Report Issue"),
            discord.SelectOption(label="Other Inquiries", description="Anything else not listed above", emoji="📩", value="Other Inquiries"),
        ]
        super().__init__(
            placeholder="Select a category to open a ticket...",
            min_values=1,
            max_values=1,
            custom_id="persistent_ticket_category_select",
            options=options
        )

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(TicketReasonModal(self.values[0]))


class TicketLauncherView(ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(TicketCategorySelect())


# ---------------- Ticket Controls: Claim, Unclaim, Close ----------------
class TicketControlView(ui.View):
    def __init__(self, is_claimed: bool = False):
        super().__init__(timeout=None)
        self.is_claimed = is_claimed
        self.update_buttons()

    def update_buttons(self):
        self.clear_items()
        if not self.is_claimed:
            btn_claim = ui.Button(label="Claim Ticket", style=discord.ButtonStyle.success, emoji="🙋", custom_id="btn_claim_ticket")
            btn_claim.callback = self.claim_ticket
            self.add_item(btn_claim)
        else:
            btn_unclaim = ui.Button(label="Unclaim Ticket", style=discord.ButtonStyle.secondary, emoji="🔄", custom_id="btn_unclaim_ticket")
            btn_unclaim.callback = self.unclaim_ticket
            self.add_item(btn_unclaim)

        btn_close = ui.Button(label="Close Ticket", style=discord.ButtonStyle.danger, emoji="🔒", custom_id="btn_close_ticket")
        btn_close.callback = self.close_ticket
        self.add_item(btn_close)

    async def claim_ticket(self, interaction: discord.Interaction):
        channel = interaction.channel
        member = interaction.user

        if not isinstance(member, discord.Member) or not is_staff(member):
            await interaction.response.send_message("Only staff members can claim tickets.", ephemeral=True)
            return

        meta = parse_topic_metadata(channel.topic)
        if meta.get("claimed_by"):
            claimed_staff = interaction.guild.get_member(meta["claimed_by"])
            name = claimed_staff.mention if claimed_staff else f"<@{meta['claimed_by']}>"
            await interaction.response.send_message(f"This ticket is already claimed by {name}.", ephemeral=True)
            return

        new_topic = f"Ticket Owner ID: {meta['owner_id']} | Type: {meta['type']} | Claimed By: {member.id}"
        await channel.edit(topic=new_topic)

        self.is_claimed = True
        self.update_buttons()
        await interaction.response.edit_message(view=self)

        embed = discord.Embed(
            description=f"Ticket has been claimed by {member.mention}. They will assist you.",
            color=discord.Color.green()
        )
        await channel.send(embed=embed)

    async def unclaim_ticket(self, interaction: discord.Interaction):
        channel = interaction.channel
        member = interaction.user

        if not isinstance(member, discord.Member):
            return

        meta = parse_topic_metadata(channel.topic)
        if member.id != meta["claimed_by"] and not member.guild_permissions.administrator:
            await interaction.response.send_message("Only the staff member who claimed this ticket can unclaim it.", ephemeral=True)
            return

        new_topic = f"Ticket Owner ID: {meta['owner_id']} | Type: {meta['type']} | Claimed By: None"
        await channel.edit(topic=new_topic)

        self.is_claimed = False
        self.update_buttons()
        await interaction.response.edit_message(view=self)

        embed = discord.Embed(
            description=f"Ticket was unclaimed by {member.mention}. Another staff member may now claim it.",
            color=discord.Color.light_grey()
        )
        await channel.send(embed=embed)

    async def close_ticket(self, interaction: discord.Interaction):
        channel = interaction.channel
        meta = parse_topic_metadata(channel.topic)

        if meta.get("owner_id"):
            owner = interaction.guild.get_member(meta["owner_id"])
            if owner:
                await channel.set_permissions(owner, view_channel=True, send_messages=False)

        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(view=self)

        closed_embed = discord.Embed(
            title="Ticket Closed",
            description=f"Ticket closed by {interaction.user.mention}.\nPress **Delete Channel** below to permanently remove it.",
            color=discord.Color.orange()
        )
        await channel.send(embed=closed_embed, view=TicketClosedActionsView())


class TicketClosedActionsView(ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @ui.button(label="Delete Channel", style=discord.ButtonStyle.danger, emoji="🗑️", custom_id="btn_delete_channel")
    async def delete_channel(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_message("Deleting ticket channel in 5 seconds...")
        await asyncio.sleep(5)
        await interaction.channel.delete()


# ---------------- Bot Events & Commands ----------------
@bot.event
async def on_ready():
    bot.add_view(TicketLauncherView())
    bot.add_view(TicketControlView(is_claimed=False))
    bot.add_view(TicketControlView(is_claimed=True))
    bot.add_view(TicketClosedActionsView())

    try:
        synced = await bot.tree.sync()
        logger.info(f"Synced {len(synced)} slash command(s).")
    except Exception as e:
        logger.error(f"Failed to sync slash commands: {e}")

    logger.info(f"Bot connected as {bot.user} (ID: {bot.user.id})")


@bot.tree.command(name="setup-tickets", description="Post the ticket panel embed in the current channel.")
@discord.app_commands.default_permissions(administrator=True)
async def setup_tickets(interaction: discord.Interaction):
    embed = discord.Embed(
        title="🎫 Support Desk",
        description="Select a topic from the menu below to open a private ticket with our team.",
        color=discord.Color.teal()
    )
    embed.set_footer(text="A private channel will be generated for your request.")
    await interaction.channel.send(embed=embed, view=TicketLauncherView())
    await interaction.response.send_message("Ticket panel deployed successfully!", ephemeral=True)


@bot.tree.command(name="add", description="Add a member to the current ticket channel.")
@discord.app_commands.describe(member="The member to add to this ticket")
async def add_member(interaction: discord.Interaction, member: discord.Member):
    if not isinstance(interaction.user, discord.Member) or not is_staff(interaction.user):
        await interaction.response.send_message("You do not have permission to use this command.", ephemeral=True)
        return

    meta = parse_topic_metadata(interaction.channel.topic)
    if not meta.get("owner_id"):
        await interaction.response.send_message("This command can only be used inside a ticket channel.", ephemeral=True)
        return

    await interaction.channel.set_permissions(member, view_channel=True, send_messages=True, read_message_history=True, attach_files=True)
    await interaction.response.send_message(f"Added {member.mention} to this ticket.")


@bot.tree.command(name="remove", description="Remove a member from the current ticket channel.")
@discord.app_commands.describe(member="The member to remove from this ticket")
async def remove_member(interaction: discord.Interaction, member: discord.Member):
    if not isinstance(interaction.user, discord.Member) or not is_staff(interaction.user):
        await interaction.response.send_message("You do not have permission to use this command.", ephemeral=True)
        return

    meta = parse_topic_metadata(interaction.channel.topic)
    if not meta.get("owner_id"):
        await interaction.response.send_message("This command can only be used inside a ticket channel.", ephemeral=True)
        return

    if member.id == meta["owner_id"]:
        await interaction.response.send_message("You cannot remove the ticket owner. Use the close button instead.", ephemeral=True)
        return

    await interaction.channel.set_permissions(member, overwrite=None)
    await interaction.response.send_message(f"Removed {member.mention} from this ticket.")


@bot.tree.command(name="set-avatar", description="Change the bot profile picture.")
@discord.app_commands.describe(image="Upload an image file directly", image_url="Or enter a direct image URL")
@discord.app_commands.default_permissions(administrator=True)
async def set_avatar(interaction: discord.Interaction, image: discord.Attachment | None = None, image_url: str | None = None):
    if not image and not image_url:
        await interaction.response.send_message("Please provide an image attachment or URL.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    target_url = image.url if image else image_url

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(target_url) as resp:
                if resp.status != 200:
                    await interaction.followup.send("Failed to fetch image from URL.", ephemeral=True)
                    return
                img_data = await resp.read()

        await bot.user.edit(avatar=img_data)
        embed = discord.Embed(title="Avatar Updated", description="Bot avatar has been updated.", color=discord.Color.green())
        if bot.user.avatar:
            embed.set_thumbnail(url=bot.user.avatar.url)
        await interaction.followup.send(embed=embed, ephemeral=True)
    except discord.HTTPException as e:
        await interaction.followup.send(f"Discord API Error: {e.text} (Rate limit: 2 changes per 10m)", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"Error: {e}", ephemeral=True)


@bot.tree.command(name="set-banner", description="Change the bot banner.")
@discord.app_commands.describe(image="Upload an image file directly", image_url="Or enter a direct image URL")
@discord.app_commands.default_permissions(administrator=True)
async def set_banner(interaction: discord.Interaction, image: discord.Attachment | None = None, image_url: str | None = None):
    if not image and not image_url:
        await interaction.response.send_message("Please provide an image attachment or URL.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    target_url = image.url if image else image_url

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(target_url) as resp:
                if resp.status != 200:
                    await interaction.followup.send("Failed to fetch image from URL.", ephemeral=True)
                    return
                img_data = await resp.read()

        await bot.user.edit(banner=img_data)
        embed = discord.Embed(title="Banner Updated", description="Bot banner has been updated.", color=discord.Color.green())
        if bot.user.banner:
            embed.set_image(url=bot.user.banner.url)
        await interaction.followup.send(embed=embed, ephemeral=True)
    except discord.HTTPException as e:
        await interaction.followup.send(f"Discord API Error: {e.text}", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"Error: {e}", ephemeral=True)


# ---------------- Railway Health Check Server ----------------
async def handle_health_check(request: web.Request) -> web.Response:
    bot_status = "online" if bot.is_ready() else "starting"
    return web.json_response({
        "status": "healthy",
        "bot": bot_status,
        "guilds": len(bot.guilds) if bot.is_ready() else 0
    })

async def run_health_server(port: int):
    app = web.Application()
    app.router.add_get("/", handle_health_check)
    app.router.add_get("/health", handle_health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info(f"Railway health check server listening on port {port}")


# ---------------- Main Entrypoint ----------------
async def main():
    if not TOKEN:
        raise ValueError("DISCORD_TOKEN environment variable is missing.")

    # Start the HTTP health server for Railway
    await run_health_server(PORT)

    # Start the Discord Bot
    async with bot:
        await bot.start(TOKEN)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot shutting down gracefully.")
  
