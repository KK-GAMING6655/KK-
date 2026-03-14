# bot.py
# Consolidated, fixed, and improved embeds for card displays.
# Env vars required: DISCORD_TOKEN; optional: GUILD_ID (int) for instant guild sync.
# requirements.txt must include: discord.py>=2.0.0, aiohttp, aiosqlite

import os
import asyncio
import random
import logging
from typing import Optional, Tuple, List

import aiosqlite
from aiohttp import web

import discord
from discord import app_commands
from discord.ext import commands
from discord.ui import View

# Logging
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("kk-bot")

# Config
DB_PATH = "cards_economy.db"
DEFAULT_RARITIES: List[Tuple[str, str, int]] = [
    ("Common", "#95a5a6", 50),
    ("Uncommon", "#2ecc71", 25),
    ("Rare", "#3498db", 12),
    ("Epic", "#8e44ad", 8),
    ("Legendary", "#f1c40f", 3),
    ("Super legendary", "#e74c3c", 2),
]

GUILD_ID_ENV = os.environ.get("GUILD_ID")
GUILD_ID: Optional[int] = None
if GUILD_ID_ENV:
    try:
        GUILD_ID = int(GUILD_ID_ENV)
    except ValueError:
        log.warning("GUILD_ID is not an integer; ignoring.")

# Bot setup
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

# Health server
async def handle(request):
    return web.Response(text="OK")

async def start_web_server():
    app = web.Application()
    app.router.add_get("/", handle)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", "8080"))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    log.info("Health server running on port %s", port)

# Database helpers
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""CREATE TABLE IF NOT EXISTS rarities (
            name TEXT PRIMARY KEY, colour_hex TEXT, weight INTEGER
        )""")
        await db.execute("""CREATE TABLE IF NOT EXISTS cards (
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE, image_url TEXT, rarity TEXT, value INTEGER
        )""")
        await db.execute("""CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY, balance INTEGER DEFAULT 0
        )""")
        await db.execute("""CREATE TABLE IF NOT EXISTS inventory (
            user_id INTEGER, card_id INTEGER, quantity INTEGER,
            PRIMARY KEY(user_id, card_id)
        )""")
        await db.execute("""CREATE TABLE IF NOT EXISTS market (
            id INTEGER PRIMARY KEY AUTOINCREMENT, seller_id INTEGER, card_id INTEGER, price INTEGER, quantity INTEGER
        )""")
        await db.execute("""CREATE TABLE IF NOT EXISTS drops (
            message_id INTEGER PRIMARY KEY, card_id INTEGER, remaining INTEGER
        )""")
        await db.execute("""CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY, value TEXT
        )""")
        await db.commit()

        cur = await db.execute("SELECT COUNT(*) FROM rarities")
        row = await cur.fetchone()
        if row is None or row[0] == 0:
            for name, colour, weight in DEFAULT_RARITIES:
                await db.execute("INSERT OR REPLACE INTO rarities (name, colour_hex, weight) VALUES (?, ?, ?)",
                                 (name, colour, weight))
            await db.commit()
            log.info("Seeded default rarities")

async def get_setting(key: str) -> Optional[str]:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT value FROM settings WHERE key = ?", (key,))
        r = await cur.fetchone()
        return r[0] if r else None

async def set_setting(key: str, value: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))
        await db.commit()

async def ensure_user(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR IGNORE INTO users (user_id, balance) VALUES (?, ?)", (user_id, 0))
        await db.commit()

# Card embed helper (rich)
async def card_embed(card_row, header: Optional[str] = None):
    # card_row: (id, name, image_url, rarity, value)
    card_id, name, image_url, rarity, value = card_row
    colour = 0x95a5a6
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT colour_hex FROM rarities WHERE name = ?", (rarity,))
        r = await cur.fetchone()
        if r and r[0]:
            try:
                colour = int(r[0].lstrip("#"), 16)
            except Exception:
                colour = 0x95a5a6

    title = f"{name} — {rarity}"
    embed = discord.Embed(title=title, color=colour)
    if header:
        embed.description = header
    # image
    if image_url:
        embed.set_image(url=image_url)
    # fields
    embed.add_field(name="Name", value=name, inline=True)
    embed.add_field(name="Rarity", value=rarity, inline=True)
    embed.add_field(name="Value", value=str(value), inline=True)
    embed.set_footer(text=f"Card ID: {card_id}")
    return embed

# Admin check
def is_admin():
    async def predicate(interaction: discord.Interaction) -> bool:
        if interaction.user.guild_permissions.administrator:
            return True
        if interaction.guild and interaction.user == interaction.guild.owner:
            return True
        await interaction.response.send_message("You must be an admin to use this command.", ephemeral=True)
        return False
    return app_commands.check(predicate)

# Views
class DropView(View):
    def __init__(self, message_id: int, card_id: int):
        super().__init__(timeout=None)
        self.message_id = message_id
        self.card_id = card_id

    @discord.ui.button(label="Get", style=discord.ButtonStyle.primary, custom_id="drop_get")
    async def get_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute("SELECT remaining FROM drops WHERE message_id = ?", (self.message_id,))
            r = await cur.fetchone()
            if not r or r[0] <= 0:
                await interaction.response.send_message("This drop is no longer available.", ephemeral=True)
                button.disabled = True
                try:
                    await interaction.message.edit(view=self)
                except Exception:
                    pass
                return
            remaining = r[0] - 1
            await db.execute("UPDATE drops SET remaining = ? WHERE message_id = ?", (remaining, self.message_id))
            await db.execute("INSERT OR IGNORE INTO users (user_id, balance) VALUES (?, ?)", (interaction.user.id, 0))
            await db.execute("""INSERT INTO inventory (user_id, card_id, quantity)
                                VALUES (?, ?, 1)
                                ON CONFLICT(user_id, card_id) DO UPDATE SET quantity = quantity + 1""",
                             (interaction.user.id, self.card_id))
            await db.commit()

        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute("SELECT id, name, image_url, rarity, value FROM cards WHERE id = ?", (self.card_id,))
            card = await cur.fetchone()

        channel_id = await get_setting("default_channel")
        if channel_id and card:
            try:
                ch = interaction.guild.get_channel(int(channel_id))
                if ch:
                    embed = await card_embed(card, header=f"{interaction.user.display_name} claimed a drop!")
                    await ch.send(embed=embed)
            except Exception:
                pass

        await interaction.response.send_message("You got the card! Check the default channel announcement.", ephemeral=True)

        if remaining <= 0:
            button.disabled = True
            try:
                await interaction.message.edit(view=self)
            except Exception:
                pass

# on_ready
@bot.event
async def on_ready():
    log.info("Logged in as %s (id: %s)", bot.user, bot.user.id)
    try:
        await init_db()
    except Exception as e:
        log.exception("init_db failed: %s", e)

    # debug list
    log.info("Commands currently defined in memory:")
    for cmd in tree.walk_commands():
        log.info(" - %s %s", cmd.name, getattr(cmd, "guild_ids", "global/guild-unknown"))

    try:
        if GUILD_ID:
            guild_obj = discord.Object(id=GUILD_ID)
            await tree.sync(guild=guild_obj)
            log.info("Commands synced to guild %s", GUILD_ID)
        else:
            await tree.sync()
            log.info("Global commands synced (may take up to 1 hour to appear).")
    except Exception as e:
        log.exception("Command sync failed: %s", e)

# -------------------- Commands --------------------
@tree.command(name="set_channel", description="Set default announcement channel for card receipts")
@is_admin()
@app_commands.describe(channel="Channel to set")
async def set_channel(interaction: discord.Interaction, channel: discord.TextChannel):
    await set_setting("default_channel", str(channel.id))
    await interaction.response.send_message(f"Default channel set to {channel.mention}.", ephemeral=True)

@tree.command(name="add_card", description="Add a card to the database")
@is_admin()
@app_commands.describe(name="Card name", image_url="Image URL", rarity="Rarity name", value="Card value")
async def add_card(interaction: discord.Interaction, name: str, image_url: str, rarity: str, value: int):
    await interaction.response.defer(ephemeral=True)
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT name FROM rarities WHERE name = ?", (rarity,))
        if not await cur.fetchone():
            await interaction.followup.send("Rarity not found.", ephemeral=True)
            return
        try:
            await db.execute("INSERT INTO cards (name, image_url, rarity, value) VALUES (?, ?, ?, ?)",
                             (name, image_url, rarity, value))
            await db.commit()
            await interaction.followup.send(f"Card **{name}** added.", ephemeral=True)
        except aiosqlite.IntegrityError:
            await interaction.followup.send("Card with that name already exists.", ephemeral=True)

@tree.command(name="remove_card", description="Remove a card")
@is_admin()
@app_commands.describe(name="Card name")
async def remove_card(interaction: discord.Interaction, name: str):
    await interaction.response.defer(ephemeral=True)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM cards WHERE name = ?", (name,))
        await db.commit()
    await interaction.followup.send(f"Card **{name}** removed (if existed).", ephemeral=True)

@tree.command(name="card_list", description="List all cards")
@is_admin()
async def card_list(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT id, name, rarity, value FROM cards")
        rows = await cur.fetchall()
    if not rows:
        await interaction.followup.send("No cards found.", ephemeral=True)
        return
    text = "\n".join([f"{r[0]}: {r[1]} ({r[2]}) - {r[3]} coins" for r in rows])
    await interaction.followup.send(f"**Cards:**\n{text}", ephemeral=True)

@tree.command(name="rarity_add", description="Add a rarity")
@is_admin()
@app_commands.describe(name="Rarity name", colour_hex="Hex colour like #ff0000", weight="Gacha weight")
async def rarity_add(interaction: discord.Interaction, name: str, colour_hex: str, weight: int):
    await interaction.response.defer(ephemeral=True)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR REPLACE INTO rarities (name, colour_hex, weight) VALUES (?, ?, ?)",
                         (name, colour_hex, weight))
        await db.commit()
    await interaction.followup.send(f"Rarity **{name}** set.", ephemeral=True)

@tree.command(name="rarity_remove", description="Remove a rarity")
@is_admin()
@app_commands.describe(name="Rarity name")
async def rarity_remove(interaction: discord.Interaction, name: str):
    await interaction.response.defer(ephemeral=True)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM rarities WHERE name = ?", (name,))
        await db.commit()
    await interaction.followup.send(f"Rarity **{name}** removed.", ephemeral=True)

@tree.command(name="rarity_list", description="List rarities")
@is_admin()
async def rarity_list(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT name, colour_hex, weight FROM rarities")
        rows = await cur.fetchall()
    text = "\n".join([f"{r[0]} - {r[1]} - weight {r[2]}" for r in rows])
    await interaction.followup.send(f"**Rarities:**\n{text}", ephemeral=True)

@tree.command(name="drop", description="Drop a card into the default channel with a Get button")
@is_admin()
@app_commands.describe(name="Card name", quantity="Number of copies to drop")
async def drop(interaction: discord.Interaction, name: str, quantity: int):
    await interaction.response.defer(ephemeral=True)
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT id, name, image_url, rarity, value FROM cards WHERE name = ?", (name,))
        card = await cur.fetchone()
        if not card:
            await interaction.followup.send("Card not found.", ephemeral=True)
            return
        channel_id = await get_setting("default_channel")
        if not channel_id:
            await interaction.followup.send("Default channel not set. Use /set_channel.", ephemeral=True)
            return
        ch = interaction.guild.get_channel(int(channel_id))
        if not ch:
            await interaction.followup.send("Default channel not found.", ephemeral=True)
            return
        embed = await card_embed(card, header=f"Drop: {card[1]} ({card[3]})")
        embed.add_field(name="Quantity", value=str(quantity), inline=True)
        msg = await ch.send(embed=embed)
        await db.execute("INSERT OR REPLACE INTO drops (message_id, card_id, remaining) VALUES (?, ?, ?)",
                         (msg.id, card[0], quantity))
        await db.commit()
        view = DropView(msg.id, card[0])
        try:
            await msg.edit(view=view)
        except Exception:
            pass
        await interaction.followup.send("Drop posted.", ephemeral=True)

@tree.command(name="luck_amount", description="Set gacha roll amount")
@is_admin()
@app_commands.describe(amount="Number of rolls per gacha command")
async def luck_amount(interaction: discord.Interaction, amount: int):
    await set_setting("luck_amount", str(amount))
    await interaction.response.send_message(f"Gacha roll amount set to {amount}.", ephemeral=True)

@tree.command(name="balance", description="Check your balance")
async def balance(interaction: discord.Interaction):
    await ensure_user(interaction.user.id)
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT balance FROM users WHERE user_id = ?", (interaction.user.id,))
        r = await cur.fetchone()
    bal = r[0] if r else 0
    await interaction.response.send_message(f"Your balance: {bal}", ephemeral=True)

@tree.command(name="gacha", description="Roll gacha to get random cards")
async def gacha(interaction: discord.Interaction):
    await ensure_user(interaction.user.id)
    await interaction.response.defer(ephemeral=True)
    amount_str = await get_setting("luck_amount") or "1"
    try:
        amount = max(1, int(amount_str))
    except Exception:
        amount = 1

    got_cards = []
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT name, weight FROM rarities")
        rarities = await cur.fetchall()
        if not rarities:
            await interaction.followup.send("No rarities configured.", ephemeral=True)
            return
        rarity_names = [r[0] for r in rarities]
        weights = [r[1] for r in rarities]
        for _ in range(amount):
            chosen = random.choices(rarity_names, weights=weights, k=1)[0]
            cur = await db.execute("SELECT id, name, image_url, rarity, value FROM cards WHERE rarity = ?", (chosen,))
            rows = await cur.fetchall()
            if not rows:
                continue
            card = random.choice(rows)
            got_cards.append(card)
            await db.execute("INSERT OR IGNORE INTO users (user_id, balance) VALUES (?, ?)", (interaction.user.id, 0))
            await db.execute("""INSERT INTO inventory (user_id, card_id, quantity)
                                VALUES (?, ?, 1)
                                ON CONFLICT(user_id, card_id) DO UPDATE SET quantity = quantity + 1""",
                             (interaction.user.id, card[0]))
        await db.commit()

    if not got_cards:
        await interaction.followup.send("No cards available for gacha.", ephemeral=True)
        return

    channel_id = await get_setting("default_channel")
    for card in got_cards:
        if channel_id:
            try:
                ch = interaction.guild.get_channel(int(channel_id))
                if ch:
                    embed = await card_embed(card, header=f"{interaction.user.display_name} rolled a gacha!")
                    await ch.send(embed=embed)
            except Exception:
                pass

    # Show a summary to the roller (ephemeral) with names and rarities
    summary = "\n".join([f"**{c[1]}** — {c[3]} (Value: {c[4]})" for c in got_cards])
    await interaction.followup.send(f"You rolled and got {len(got_cards)} card(s):\n{summary}", ephemeral=True)

@tree.command(name="view_card", description="View a card (ephemeral)")
@app_commands.describe(name="Card name")
async def view_card(interaction: discord.Interaction, name: str):
    await interaction.response.defer(ephemeral=True)
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT id, name, image_url, rarity, value FROM cards WHERE name = ?", (name,))
        card = await cur.fetchone()
    if not card:
        await interaction.followup.send("Card not found.", ephemeral=True)
        return
    embed = await card_embed(card)
    await interaction.followup.send(embed=embed, ephemeral=True)

@tree.command(name="market", description="View market listings")
async def market(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""SELECT m.id, c.name, m.price, m.quantity, m.seller_id FROM market m
                                  JOIN cards c ON c.id = m.card_id""")
        rows = await cur.fetchall()
    if not rows:
        await interaction.followup.send("Market is empty.", ephemeral=True)
        return
    text = "\n".join([f"Listing {r[0]}: {r[1]} x{r[3]} - {r[2]} coins (seller ID {r[4]})" for r in rows])
    await interaction.followup.send(f"**Market:**\n{text}", ephemeral=True)

@tree.command(name="sell", description="List a card on the market")
@app_commands.describe(card_name="Card name", price="Price per card", quantity="Quantity to sell")
async def sell(interaction: discord.Interaction, card_name: str, price: int, quantity: int):
    await interaction.response.defer(ephemeral=True)
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT id FROM cards WHERE name = ?", (card_name,))
        card = await cur.fetchone()
        if not card:
            await interaction.followup.send("Card not found.", ephemeral=True)
            return
        card_id = card[0]
        cur = await db.execute("SELECT quantity FROM inventory WHERE userid = ? AND cardid = ?", (interaction.user.id, card_id))
        r = await cur.fetchone()
        if not r or r[0] < quantity:
            await interaction.followup.send("You don't have enough cards to sell.", ephemeral=True)
            return
        await db.execute("UPDATE inventory SET quantity = quantity - ? WHERE userid = ? AND cardid = ?", (quantity, interaction.user.id, card_id))
        await db.execute("INSERT INTO market (sellerid, cardid, price, quantity) VALUES (?, ?, ?, ?)",
                         (interaction.user.id, card_id, price, quantity))
        await db.commit()
    await interaction.followup.send("Listed on market.", ephemeral=True)

@tree.command(name="buy", description="Buy from market listing")
@app_commands.describe(listing_id="Listing ID", quantity="Quantity to buy")
async def buy(interaction: discord.Interaction, listing_id: int, quantity: int):
    await interaction.response.defer(ephemeral=True)
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT sellerid, cardid, price, quantity FROM market WHERE id = ?", (listing_id,))
        row = await cur.fetchone()
        if not row:
            await interaction.followup.send("Listing not found.", ephemeral=True)
            return
        sellerid, cardid, price, avail = row
        if quantity > avail:
            await interaction.followup.send("Not enough quantity available.", ephemeral=True)
            return
        total = price * quantity
        await ensure_user(interaction.user.id)
        cur = await db.execute("SELECT balance FROM users WHERE user_id = ?", (interaction.user.id,))
        r = await cur.fetchone()
        bal = r[0] if r else 0
        if bal < total:
            await interaction.followup.send("You don't have enough balance.", ephemeral=True)
            return
        await db.execute("UPDATE users SET balance = balance - ? WHERE user_id = ?", (total, interaction.user.id))
        await db.execute("INSERT OR IGNORE INTO users (userid, balance) VALUES (?, ?)", (sellerid, 0))
        await db.execute("UPDATE users SET balance = balance + ? WHERE userid = ?", (total, sellerid))
        await db.execute("UPDATE market SET quantity = quantity - ? WHERE id = ?", (quantity, listing_id))
        await db.execute("""INSERT INTO inventory (userid, cardid, quantity) VALUES (?, ?, ?)
                            ON CONFLICT(userid, cardid) DO UPDATE SET quantity = quantity + ?""",
                         (interaction.user.id, card_id, quantity, quantity))
        await db.execute("DELETE FROM market WHERE id = ? AND quantity <= 0", (listing_id,))
        await db.commit()
        cur = await db.execute("SELECT id, name, imageurl, rarity, value FROM cards WHERE id = ?", (cardid,))
        card = await cur.fetchone()

    channelid = await getsetting("default_channel")
    if channel_id and card:
        try:
            ch = interaction.guild.getchannel(int(channelid))
            if ch:
                embed = await cardembed(card, header=f"{interaction.user.displayname} bought {card[1]}!")
                await ch.send(embed=embed)
        except Exception:
            pass

    await interaction.followup.send("Purchase completed.", ephemeral=True)

# Main entrypoint
async def main():
    await start_web_server()
    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        raise RuntimeError("DISCORD_TOKEN not set in environment")
    await bot.start(token)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
