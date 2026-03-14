# bot.py 
import os
import asyncio
from aiohttp import web
import discord
from discord.ext import commands

# ---------- Bot setup ----------
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents} (id: {bot.user.id})")

# ---------- tiny web server for health checks ----------
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
    print(f"Health server running on port {port}")

# ---------- main: start web server and bot in same loop ----------
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

ADMIN_PERMS = discord.Permissions(administrator=True)

DEFAULT_RARITIES = [
    ("Common", "#95a5a6", 50),
    ("Uncommon", "#2ecc71", 25),
    ("Rare", "#3498db", 12),
    ("Epic", "#8e44ad", 8),
    ("Legendary", "#f1c40f", 3),
    ("Super legendary", "#e74c3c", 2),
]

DB_PATH = "cards_economy.db"

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

# ---------- Utilities ----------
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

        # seed rarities if empty
        cur = await db.execute("SELECT COUNT(*) FROM rarities")
        row = await cur.fetchone()
        if row[0] == 0:
            for name, colour, weight in DEFAULT_RARITIES:
                await db.execute("INSERT INTO rarities (name, colour_hex, weight) VALUES (?, ?, ?)",
                                 (name, colour, weight))
            await db.commit()

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

# ---------- Helper: embed for a card ----------
async def card_embed(card_row, method: str = None, actor: Optional[discord.User] = None, seller: Optional[discord.User] = None):
    # card_row: (id, name, image_url, rarity, value)
    card_id, name, image_url, rarity, value = card_row
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT colour_hex FROM rarities WHERE name = ?", (rarity,))
        r = await cur.fetchone()
        colour = int(r[0].lstrip("#"), 16) if r else 0x95a5a6
    title = f"**{name}** ({rarity})"
    embed = discord.Embed(title=title, color=colour)
    embed.set_image(url=image_url or discord.Embed.Empty)
    embed.add_field(name="Value", value=str(value), inline=True)
    embed.add_field(name="Card ID", value=str(card_id), inline=True)
    if method:
        header = f"Congratulations 🎉 {actor.mention} got **{name}** ({rarity}) by {method}"
        if seller:
            header += f" from {seller.display_name}"
        embed.description = header
    return embed

# ---------- Admin check ----------
def is_admin():
    async def predicate(interaction: discord.Interaction) -> bool:
        if interaction.user.guild_permissions.administrator:
            return True
        # allow guild owner
        if interaction.user == interaction.guild.owner:
            return True
        await interaction.response.send_message("You must be an admin to use this command.", ephemeral=True)
        return False
    return app_commands.check(predicate)

# ---------- Views for drop and trade ----------
class DropView(View):
    def __init__(self, message_id: int, card_id: int):
        super().__init__(timeout=None)
        self.message_id = message_id
        self.card_id = card_id

    @discord.ui.button(label="Get", style=discord.ButtonStyle.primary, custom_id="drop_get")
    async def get_button(self, interaction: discord.Interaction, button: Button):
        # atomic check and reduce
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute("SELECT remaining FROM drops WHERE message_id = ?", (self.message_id,))
            r = await cur.fetchone()
            if not r or r[0] <= 0:
                await interaction.response.send_message("This drop is no longer available.", ephemeral=True)
                # disable button
                button.disabled = True
                await interaction.message.edit(view=self)
                return
            remaining = r[0] - 1
            await db.execute("UPDATE drops SET remaining = ? WHERE message_id = ?", (remaining, self.message_id))
            # add to user inventory
            await db.execute("INSERT OR IGNORE INTO users (user_id, balance) VALUES (?, ?)", (interaction.user.id, 0))
            await db.execute("""INSERT INTO inventory (user_id, card_id, quantity)
                                VALUES (?, ?, 1)
                                ON CONFLICT(user_id, card_id) DO UPDATE SET quantity = quantity + 1""",
                             (interaction.user.id, self.card_id))
            await db.commit()
        # fetch card for embed
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute("SELECT id, name, image_url, rarity, value FROM cards WHERE id = ?", (self.card_id,))
            card = await cur.fetchone()
        # announce in default channel
        channel_id = await get_setting("default_channel")
        if channel_id:
            ch = interaction.guild.get_channel(int(channel_id))
            if ch:
                embed = discord.Embed(title=f"{interaction.user.display_name} got '{card[1]}' ({card[3]}) from drop",
                                      color=int((await db.execute("SELECT colour_hex FROM rarities WHERE name = ?", (card[3],))).fetchone()[0].lstrip("#"), 16))
                embed.set_image(url=card[2])
                await ch.send(embed=embed)
        await interaction.response.send_message("You got the card! Check the default channel announcement.", ephemeral=True)
        if remaining <= 0:
            # disable button
            button.disabled = True
            await interaction.message.edit(view=self)

class TradeView(View):
    def __init__(self, trade_id: int, seller: discord.User, card_row, price: int):
        super().__init__(timeout=60*60)  # 1 hour to accept
        self.trade_id = trade_id
        self.seller = seller
        self.card_row = card_row
        self.price = price

    @discord.ui.button(label="✅Accept", style=discord.ButtonStyle.success, custom_id="trade_accept")
    async def accept(self, interaction: discord.Interaction, button: Button):
        buyer = interaction.user
        # check balance
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute("SELECT balance FROM users WHERE user_id = ?", (buyer.id,))
            r = await cur.fetchone()
            bal = r[0] if r else 0
            if bal < self.price:
                await interaction.response.send_message("You don't have enough balance to accept this trade.", ephemeral=True)
                return
            # transfer coins
            await db.execute("UPDATE users SET balance = balance - ? WHERE user_id = ?", (self.price, buyer.id))
            await db.execute("INSERT OR IGNORE INTO users (user_id, balance) VALUES (?, ?)", (self.seller.id, 0))
            await db.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (self.price, self.seller.id))
            # transfer card: remove from seller inventory, add to buyer
            card_id = self.card_row[0]
            # check seller has the card
            cur = await db.execute("SELECT quantity FROM inventory WHERE user_id = ? AND card_id = ?", (self.seller.id, card_id))
            r = await cur.fetchone()
            if not r or r[0] <= 0:
                await interaction.response.send_message("Seller no longer has the card.", ephemeral=True)
                return
            await db.execute("UPDATE inventory SET quantity = quantity - 1 WHERE user_id = ? AND card_id = ?", (self.seller.id, card_id))
            await db.execute("""INSERT INTO inventory (user_id, card_id, quantity)
                                VALUES (?, ?, 1)
                                ON CONFLICT(user_id, card_id) DO UPDATE SET quantity = quantity + 1""",
                             (buyer.id, card_id))
            # remove trade record if any
            await db.execute("DELETE FROM market WHERE seller_id = ? AND card_id = ? AND price = ? LIMIT 1", (self.seller.id, card_id, self.price))
            await db.commit()
        # announce in default channel
        channel_id = await get_setting("default_channel")
        if channel_id:
            ch = interaction.guild.get_channel(int(channel_id))
            if ch:
                embed = await card_embed(self.card_row, method="trade", actor=buyer, seller=self.seller)
                await ch.send(embed=embed)
        await interaction.response.send_message("Trade accepted and completed.", ephemeral=True)
        self.stop()

    @discord.ui.button(label="❌Deny", style=discord.ButtonStyle.danger, custom_id="trade_deny")
    async def deny(self, interaction: discord.Interaction, button: Button):
        await interaction.response.send_message("You denied the trade.", ephemeral=True)
        self.stop()

# ---------- Bot events ----------
@bot.event
async def on_ready():
    await init_db()
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    try:
        if GUILD_ID:
            guild = discord.Object(id=int(GUILD_ID))
            await tree.sync(guild=guild)
        else:
            await tree.sync()
        print("Slash commands synced.")
    except Exception as e:
        print("Sync failed:", e)

tree = bot.tree

# ---------- Admin commands ----------
@tree.command(name="add_card", description="Add a card to the database")
@is_admin()
@app_commands.describe(name="Card name", image_url="Image URL", rarity="Rarity name", value="Card value")
async def add_card(interaction: discord.Interaction, name: str, image_url: str, rarity: str, value: int):
    await interaction.response.defer(ephemeral=True)
    async with aiosqlite.connect(DB_PATH) as db:
        # check rarity exists
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

@tree.command(name="embed_colour", description="Set embed colour for a rarity")
@is_admin()
@app_commands.describe(rarity="Rarity name", colour_hex="Hex colour like #ff0")
async def embed_colour(interaction: discord.Interaction, rarity: str, colour_hex: str):
    await interaction.response.defer(ephemeral=True)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE rarities SET colour_hex = ? WHERE name = ?", (colour_hex, rarity))
        await db.commit()
    await interaction.followup.send(f"Embed colour for **{rarity}** set to {colour_hex}.", ephemeral=True)

@tree.command(name="clear_inventory", description="Clear inventory of a user")
@is_admin()
@app_commands.describe(user="Target user")
async def clear_inventory(interaction: discord.Interaction, user: discord.Member):
    await interaction.response.defer(ephemeral=True)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM inventory WHERE user_id = ?", (user.id,))
        await db.commit()
    await interaction.followup.send(f"Cleared inventory of {user.display_name}.", ephemeral=True)

@tree.command(name="clear_balance", description="Clear balance of a user")
@is_admin()
@app_commands.describe(user="Target user")
async def clear_balance(interaction: discord.Interaction, user: discord.Member):
    await interaction.response.defer(ephemeral=True)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET balance = 0 WHERE user_id = ?", (user.id,))
        await db.commit()
    await interaction.followup.send(f"Cleared balance of {user.display_name}.", ephemeral=True)

@tree.command(name="inspect_inventory", description="Inspect a user's inventory (ephemeral)")
@is_admin()
@app_commands.describe(user="Target user")
async def inspect_inventory(interaction: discord.Interaction, user: discord.Member):
    await interaction.response.defer(ephemeral=True)
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""SELECT c.name, i.quantity FROM inventory i
                                  JOIN cards c ON c.id = i.card_id
                                  WHERE i.user_id = ?""", (user.id,))
        rows = await cur.fetchall()
    if not rows:
        await interaction.followup.send("No items.", ephemeral=True)
        return
    text = "\n".join([f"{r[0]} x{r[1]}" for r in rows])
    await interaction.followup.send(f"Inventory of {user.display_name}:\n{text}", ephemeral=True)

@tree.command(name="add_coin", description="Add coins to a user")
@is_admin()
@app_commands.describe(user="Target user", amount="Amount to add")
async def add_coin(interaction: discord.Interaction, user: discord.Member, amount: int):
    await interaction.response.defer(ephemeral=True)
    await ensure_user(user.id)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (amount, user.id))
        await db.commit()
    await interaction.followup.send(f"Added {amount} coins to {user.display_name}.", ephemeral=True)

@tree.command(name="remove_coin", description="Remove coins from a user")
@is_admin()
@app_commands.describe(user="Target user", amount="Amount to remove")
async def remove_coin(interaction: discord.Interaction, user: discord.Member, amount: int):
    await interaction.response.defer(ephemeral=True)
    await ensure_user(user.id)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET balance = MAX(balance - ?, 0) WHERE user_id = ?", (amount, user.id))
        await db.commit()
    await interaction.followup.send(f"Removed {amount} coins from {user.display_name}.", ephemeral=True)

@tree.command(name="set_channel", description="Set default announcement channel for card receipts")
@is_admin()
@app_commands.describe(channel="Channel to set")
async def set_channel(interaction: discord.Interaction, channel: discord.TextChannel):
    await set_setting("default_channel", str(channel.id))
    await interaction.response.send_message(f"Default channel set to {channel.mention}.", ephemeral=True)

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
        embed = discord.Embed(title=f"Drop: {card[1]} ({card[3]})", color=int((await db.execute("SELECT colour_hex FROM rarities WHERE name = ?", (card[3],))).fetchone()[0].lstrip("#"), 16))
        embed.set_image(url=card[2])
        embed.add_field(name="Quantity", value=str(quantity))
        msg = await ch.send(embed=embed)
        await db.execute("INSERT OR REPLACE INTO drops (message_id, card_id, remaining) VALUES (?, ?, ?)",
                         (msg.id, card[0], quantity))
        await db.commit()
        view = DropView(msg.id, card[0])
        await msg.edit(view=view)
        await interaction.followup.send("Drop posted.", ephemeral=True)

@tree.command(name="luck_amount", description="Set gacha roll amount")
@is_admin()
@app_commands.describe(amount="Number of rolls per gacha command")
async def luck_amount(interaction: discord.Interaction, amount: int):
    await set_setting("luck_amount", str(amount))
    await interaction.response.send_message(f"Gacha roll amount set to {amount}.", ephemeral=True)

# ---------- Member commands ----------
@tree.command(name="balance", description="Check your balance")
async def balance(interaction: discord.Interaction):
    await ensure_user(interaction.user.id)
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT balance FROM users WHERE user_id = ?", (interaction.user.id,))
        r = await cur.fetchone()
    await interaction.response.send_message(f"Your balance: {r[0]}", ephemeral=True)

@tree.command(name="gacha", description="Roll gacha to get random cards")
async def gacha(interaction: discord.Interaction):
    await ensure_user(interaction.user.id)
    await interaction.response.defer(ephemeral=True)
    amount = await get_setting("luck_amount") or "1"
    amount = int(amount)
    # build rarity pool
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT name, weight FROM rarities")
        rarities = await cur.fetchall()
        if not rarities:
            await interaction.followup.send("No rarities configured.", ephemeral=True)
            return
        rarity_names = [r[0] for r in rarities]
        weights = [r[1] for r in rarities]
        # pick rarities then pick random card from that rarity
        got_cards = []
        for _ in range(amount):
            chosen = random.choices(rarity_names, weights=weights, k=1)[0]
            cur = await db.execute("SELECT id, name, image_url, rarity, value FROM cards WHERE rarity = ?", (chosen,))
            rows = await cur.fetchall()
            if not rows:
                continue
            card = random.choice(rows)
            got_cards.append(card)
            # add to inventory
            await db.execute("INSERT OR IGNORE INTO users (user_id, balance) VALUES (?, ?)", (interaction.user.id, 0))
            await db.execute("""INSERT INTO inventory (user_id, card_id, quantity)
                                VALUES (?, ?, 1)
                                ON CONFLICT(user_id, card_id) DO UPDATE SET quantity = quantity + 1""",
                             (interaction.user.id, card[0]))
        await db.commit()
    if not got_cards:
        await interaction.followup.send("No cards available for gacha.", ephemeral=True)
        return
    # announce in default channel each card
    channel_id = await get_setting("default_channel")
    for card in got_cards:
        if channel_id:
            ch = interaction.guild.get_channel(int(channel_id))
            if ch:
                embed = await card_embed(card, method="gacha", actor=interaction.user)
                await ch.send(embed=embed)
    await interaction.followup.send(f"You rolled and got {len(got_cards)} card(s). Check the default channel.", ephemeral=True)

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
    embed = discord.Embed(title=f"{card[1]} ({card[3]})", color=int((await aiosqlite.connect(DB_PATH)).execute("SELECT colour_hex FROM rarities WHERE name = ?", (card[3],)).fetchone()[0].lstrip("#"), 16))
    embed.set_image(url=card[2])
    embed.add_field(name="Value", value=str(card[4]))
    await interaction.followup.send(embed=embed, ephemeral=True)

@tree.command(name="market", description="View market listings")
async def market(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""SELECT m.id, c.name, m.price, m.quantity, u.user_id FROM market m
                                  JOIN cards c ON c.id = m.card_id
                                  JOIN users u ON u.user_id = m.seller_id""")
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
        # find card
        cur = await db.execute("SELECT id FROM cards WHERE name = ?", (card_name,))
        card = await cur.fetchone()
        if not card:
            await interaction.followup.send("Card not found.", ephemeral=True)
            return
        card_id = card[0]
        # check user has enough
        cur = await db.execute("SELECT quantity FROM inventory WHERE user_id = ? AND card_id = ?", (interaction.user.id, card_id))
        r = await cur.fetchone()
        if not r or r[0] < quantity:
            await interaction.followup.send("You don't have enough cards to sell.", ephemeral=True)
            return
        # reduce inventory and add market listing
        await db.execute("UPDATE inventory SET quantity = quantity - ? WHERE user_id = ? AND card_id = ?", (quantity, interaction.user.id, card_id))
        await db.execute("INSERT INTO market (seller_id, card_id, price, quantity) VALUES (?, ?, ?, ?)",
                         (interaction.user.id, card_id, price, quantity))
        await db.commit()
    await interaction.followup.send("Listed on market.", ephemeral=True)

@tree.command(name="buy", description="Buy from market listing")
@app_commands.describe(listing_id="Listing ID", quantity="Quantity to buy")
async def buy(interaction: discord.Interaction, listing_id: int, quantity: int):
    await interaction.response.defer(ephemeral=True)
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT seller_id, card_id, price, quantity FROM market WHERE id = ?", (listing_id,))
        row = await cur.fetchone()
        if not row:
            await interaction.followup.send("Listing not found.", ephemeral=True)
            return
        seller_id, card_id, price, avail = row
        if quantity > avail:
            await interaction.followup.send("Not enough quantity available.", ephemeral=True)
            return
        total = price * quantity
        await ensure_user(interaction.user.id)
        cur = await db.execute("SELECT balance FROM users WHERE user_id = ?", (interaction.user.id,))
        bal = (await cur.fetchone())[0]
        if bal < total:
            await interaction.followup.send("You don't have enough balance.", ephemeral=True)
            return
        # transfer coins and cards
        await db.execute("UPDATE users SET balance = balance - ? WHERE user_id = ?", (total, interaction.user.id))
        await db.execute("INSERT OR IGNORE INTO users (user_id, balance) VALUES (?, ?)", (seller_id, 0))
        await db.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (total, seller_id))
        await db.execute("UPDATE market SET quantity = quantity - ? WHERE id = ?", (quantity, listing_id))
        await db.execute("INSERT INTO inventory (user_id, card_id, quantity) VALUES (?, ?, ?) ON CONFLICT(user_id, card_id) DO UPDATE SET quantity = quantity + ?",
                         (interaction.user.id, card_id, quantity, quantity))
        # remove listing if zero
        await db.execute("DELETE FROM market WHERE id = ? AND quantity <= 0", (listing_id,))
        await db.commit()
        # fetch card for embed
        cur = await db.execute("SELECT id, name, image_url, rarity, value FROM cards WHERE id = ?", (card_id,))
        card = await cur.fetchone()
    # announce in default channel
    channel_id = await get_setting("default_channel")
    if channel_id:
        ch = interaction.guild.get_channel(int(channel_id))
        if ch:
            embed = await card_embed(card, method="buy", actor=interaction.user, seller=bot.get_user(seller_id))
            await ch.send(embed=embed)
    await interaction.followup.send("Purchase successful.", ephemeral=True)

@tree.command(name="trade", description="Offer a trade to another user")
@app_commands.describe(user="Receiver", card_name="Name of card to trade", price="Price buyer must pay")
async def trade(interaction: discord.Interaction, user: discord.Member, card_name: str, price: int):
    await interaction.response.defer(ephemeral=True)
    # check seller has the card
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT id, name, image_url, rarity, value FROM cards WHERE name = ?", (card_name,))
        card = await cur.fetchone()
        if not card:
            await interaction.followup.send("Card not found.", ephemeral=True)
            return
        cur = await db.execute("SELECT quantity FROM inventory WHERE user_id = ? AND card_id = ?", (interaction.user.id, card[0]))
        r = await cur.fetchone()
        if not r or r[0] <= 0:
            await interaction.followup.send("You don't have that card to trade.", ephemeral=True)
            return
    # send DM to receiver with embed and buttons
    try:
        view = TradeView(trade_id=0, seller=interaction.user, card_row=card, price=price)
        embed = discord.Embed(title=f"Trade offer: {card[1]} ({card[3]})", description=f"{interaction.user.display_name} offers this card for {price} coins. Accept to buy it.", color=int((await aiosqlite.connect(DB_PATH)).execute("SELECT colour_hex FROM rarities WHERE name = ?", (card[3],)).fetchone()[0].lstrip("#"), 16))
        embed.set_image(url=card[2])
        await user.send(embed=embed, view=view)
        await interaction.followup.send("Trade offer sent (check receiver's DMs).", ephemeral=True)
    except discord.Forbidden:
        await interaction.followup.send("Could not DM the user. They may have DMs closed.", ephemeral=True)

@tree.command(name="give_card", description="Give a card to a user for free")
@app_commands.describe(user="Receiver", name="Card name")
async def give_card(interaction: discord.Interaction, user: discord.Member, name: str):
    await interaction.response.defer(ephemeral=True)
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT id FROM cards WHERE name = ?", (name,))
        card = await cur.fetchone()
        if not card:
            await interaction.followup.send("Card not found.", ephemeral=True)
            return
        await db.execute("INSERT OR IGNORE INTO users (user_id, balance) VALUES (?, ?)", (user.id, 0))
        await db.execute("""INSERT INTO inventory (user_id, card_id, quantity)
                            VALUES (?, ?, 1)
                            ON CONFLICT(user_id, card_id) DO UPDATE SET quantity = quantity + 1""",
                         (user.id, card[0]))
        await db.commit()
        # announce
        cur = await db.execute("SELECT id, name, image_url, rarity, value FROM cards WHERE id = ?", (card[0],))
        card_row = await cur.fetchone()
    channel_id = await get_setting("default_channel")
    if channel_id:
        ch = interaction.guild.get_channel(int(channel_id))
        if ch:
            embed = await card_embed(card_row, method="give", actor=user, seller=interaction.user)
            await ch.send(embed=embed)
    await interaction.followup.send(f"Gave {name} to {user.display_name}.", ephemeral=True)

@tree.command(name="give_coin", description="Give coins to a user (from your balance)")
@app_commands.describe(user="Receiver", amount="Amount to give")
async def give_coin(interaction: discord.Interaction, user: discord.Member, amount: int):
    await interaction.response.defer(ephemeral=True)
    await ensure_user(interaction.user.id)
    await ensure_user(user.id)
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT balance FROM users WHERE user_id = ?", (interaction.user.id,))
        bal = (await cur.fetchone())[0]
        if bal < amount:
            await interaction.followup.send("You don't have enough balance.", ephemeral=True)
            return
        await db.execute("UPDATE users SET balance = balance - ? WHERE user_id = ?", (amount, interaction.user.id))
        await db.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (amount, user.id))
        await db.commit()
    await interaction.followup.send(f"Gave {amount} coins to {user.display_name}.", ephemeral=True)

# ---------- Run ----------
if __name__ == "__main__":
    if not DISCORD_TOKEN:
        print("Set DISCORD_TOKEN environment variable.")
    else:
        bot.run(DISCORD_TOKEN)
