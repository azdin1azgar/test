#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════╗
║              MemeSol Wallet — Telegram Bot                   ║
║   Simulated Solana Meme Coin Trading Platform                ║
║   Python 3.12 | python-telegram-bot | SQLite | Async        ║
╚══════════════════════════════════════════════════════════════╝

Author  : Senior Python / Telegram Bot / Solana Developer
Version : 1.0.0
License : MIT
"""

# ─────────────────────────────────────────────
#  Standard Library
# ─────────────────────────────────────────────
import asyncio
import hashlib
import logging
import os
import random
import re
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from functools import wraps
from typing import Optional

# ─────────────────────────────────────────────
#  Third-party
#  pip install python-telegram-bot==20.*
# ─────────────────────────────────────────────
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

# ═══════════════════════════════════════════════════════════════
#  CONFIGURATION
# ═══════════════════════════════════════════════════════════════

# ──────────── Replace with your actual BotFather token ────────
BOT_TOKEN: str = "YOUR_BOT_TOKEN_HERE"

# ──────────── Database file ───────────────────────────────────
DB_PATH: str = "memesol.db"

# ──────────── Demo bonus given to every new user (USD) ────────
DEMO_BONUS_USD: float = 300.0

# ──────────── Minimum deposit required for activation (USD) ──
MIN_ACTIVATION_USD: float = 10.0

# ──────────── Anti-spam: max messages per window ──────────────
SPAM_LIMIT: int = 8          # max actions
SPAM_WINDOW: int = 10        # seconds

# ──────────── Simulated SOL price (USD) for demo purposes ─────
SOL_PRICE_USD: float = 145.0

# ──────────── Meme coin catalogue ─────────────────────────────
# Each coin: symbol → {name, price_usd, change_24h (%), emoji}
COINS: dict = {
    "BONK": {"name": "Bonk",    "price": 0.000028, "change": +12.4, "emoji": "🐕"},
    "WIF":  {"name": "dogwifhat","price": 2.85,     "change": -3.1,  "emoji": "🎩"},
    "POPCAT":{"name":"Popcat",  "price": 0.92,      "change": +7.8,  "emoji": "🐱"},
    "MYRO": {"name": "Myro",    "price": 0.087,     "change": -1.5,  "emoji": "🐶"},
    "BOME": {"name": "Book of Meme","price": 0.0095,"change": +21.3, "emoji": "📚"},
}

# ──────────── Conversation states ─────────────────────────────
(
    STATE_TRADE_COIN,
    STATE_TRADE_ACTION,
    STATE_TRADE_AMOUNT,
    STATE_DEPOSIT_ADDRESS,
    STATE_WITHDRAW_ADDRESS,
    STATE_WITHDRAW_AMOUNT,
    STATE_SETTINGS,
) = range(7)

# ─────────────────────────────────────────────
#  LOGGING
# ─────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    level=logging.INFO,
)
logger = logging.getLogger("MemeSolBot")


# ═══════════════════════════════════════════════════════════════
#  DATABASE LAYER
# ═══════════════════════════════════════════════════════════════

def get_connection() -> sqlite3.Connection:
    """Return a thread-safe SQLite connection with row factory."""
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")   # better concurrency
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    """Create all tables if they do not exist."""
    conn = get_connection()
    cur = conn.cursor()

    # ── Users ──────────────────────────────────────────────────
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id     INTEGER UNIQUE NOT NULL,
            user_uid        TEXT    UNIQUE NOT NULL,   -- public-facing UID
            username        TEXT,
            first_name      TEXT,
            sol_balance     REAL    DEFAULT 0.0,
            usdt_balance    REAL    DEFAULT 0.0,
            demo_bonus      REAL    DEFAULT 300.0,
            is_activated    INTEGER DEFAULT 0,         -- 0=No, 1=Yes
            has_traded      INTEGER DEFAULT 0,         -- 0=No, 1=Yes
            total_deposited REAL    DEFAULT 0.0,
            created_at      TEXT    NOT NULL,
            last_seen       TEXT    NOT NULL
        )
    """)

    # ── Coin Holdings ─────────────────────────────────────────
    cur.execute("""
        CREATE TABLE IF NOT EXISTS holdings (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER NOT NULL,
            symbol      TEXT    NOT NULL,
            quantity    REAL    DEFAULT 0.0,
            avg_buy_price REAL  DEFAULT 0.0,
            UNIQUE(telegram_id, symbol),
            FOREIGN KEY(telegram_id) REFERENCES users(telegram_id)
        )
    """)

    # ── Trade History ─────────────────────────────────────────
    cur.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER NOT NULL,
            symbol      TEXT    NOT NULL,
            action      TEXT    NOT NULL,   -- BUY | SELL
            quantity    REAL    NOT NULL,
            price_usd   REAL    NOT NULL,
            total_usd   REAL    NOT NULL,
            pnl_usd     REAL    DEFAULT 0.0,
            source      TEXT    DEFAULT 'demo', -- demo | live
            created_at  TEXT    NOT NULL,
            FOREIGN KEY(telegram_id) REFERENCES users(telegram_id)
        )
    """)

    # ── Deposit Requests ──────────────────────────────────────
    cur.execute("""
        CREATE TABLE IF NOT EXISTS deposits (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id     INTEGER NOT NULL,
            deposit_address TEXT    NOT NULL,
            amount_sol      REAL    DEFAULT 0.0,
            amount_usd      REAL    DEFAULT 0.0,
            status          TEXT    DEFAULT 'pending', -- pending|confirmed|failed
            created_at      TEXT    NOT NULL,
            FOREIGN KEY(telegram_id) REFERENCES users(telegram_id)
        )
    """)

    # ── Withdrawal Requests ───────────────────────────────────
    cur.execute("""
        CREATE TABLE IF NOT EXISTS withdrawals (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id     INTEGER NOT NULL,
            to_address      TEXT    NOT NULL,
            amount_sol      REAL    NOT NULL,
            amount_usd      REAL    NOT NULL,
            status          TEXT    DEFAULT 'pending',
            created_at      TEXT    NOT NULL,
            FOREIGN KEY(telegram_id) REFERENCES users(telegram_id)
        )
    """)

    conn.commit()
    conn.close()
    logger.info("Database initialised at %s", DB_PATH)


# ─────────────────────────────────────────────
#  DB Helper Functions
# ─────────────────────────────────────────────

def now_iso() -> str:
    """Return current UTC timestamp as ISO-8601 string."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def generate_uid() -> str:
    """Generate a short unique user ID (8 uppercase hex chars)."""
    return uuid.uuid4().hex[:8].upper()


def generate_sol_address() -> str:
    """
    Generate a fake-but-realistic Solana-style base58 address
    for demo purposes only.
    """
    chars = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    return "".join(random.choices(chars, k=44))


def get_user(telegram_id: int) -> Optional[sqlite3.Row]:
    """Fetch a user row by Telegram ID; returns None if not found."""
    conn = get_connection()
    row = conn.execute(
        "SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)
    ).fetchone()
    conn.close()
    return row


def create_user(telegram_id: int, username: str, first_name: str) -> sqlite3.Row:
    """Register a brand-new user and return the created row."""
    uid = generate_uid()
    ts  = now_iso()
    conn = get_connection()
    conn.execute(
        """
        INSERT INTO users
            (telegram_id, user_uid, username, first_name,
             sol_balance, usdt_balance, demo_bonus,
             is_activated, has_traded, total_deposited,
             created_at, last_seen)
        VALUES (?,?,?,?, 0,0,?,  0,0,0, ?,?)
        """,
        (telegram_id, uid, username, first_name,
         DEMO_BONUS_USD, ts, ts),
    )
    conn.commit()
    row = conn.execute(
        "SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)
    ).fetchone()
    conn.close()
    logger.info("New user registered: %s (tg=%s)", uid, telegram_id)
    return row


def update_last_seen(telegram_id: int) -> None:
    """Bump last_seen timestamp for a user."""
    conn = get_connection()
    conn.execute(
        "UPDATE users SET last_seen=? WHERE telegram_id=?",
        (now_iso(), telegram_id),
    )
    conn.commit()
    conn.close()


def get_holding(telegram_id: int, symbol: str) -> Optional[sqlite3.Row]:
    """Return a user's holding row for a specific coin."""
    conn = get_connection()
    row = conn.execute(
        "SELECT * FROM holdings WHERE telegram_id=? AND symbol=?",
        (telegram_id, symbol),
    ).fetchone()
    conn.close()
    return row


def upsert_holding(
    telegram_id: int, symbol: str, qty_delta: float, new_avg: float
) -> None:
    """
    Insert or update a coin holding.
    qty_delta is positive for buys, negative for sells.
    """
    conn = get_connection()
    existing = conn.execute(
        "SELECT quantity FROM holdings WHERE telegram_id=? AND symbol=?",
        (telegram_id, symbol),
    ).fetchone()
    if existing:
        new_qty = existing["quantity"] + qty_delta
        conn.execute(
            "UPDATE holdings SET quantity=?, avg_buy_price=? "
            "WHERE telegram_id=? AND symbol=?",
            (max(new_qty, 0), new_avg, telegram_id, symbol),
        )
    else:
        conn.execute(
            "INSERT INTO holdings (telegram_id, symbol, quantity, avg_buy_price) "
            "VALUES (?,?,?,?)",
            (telegram_id, symbol, max(qty_delta, 0), new_avg),
        )
    conn.commit()
    conn.close()


def record_trade(
    telegram_id: int,
    symbol: str,
    action: str,
    quantity: float,
    price_usd: float,
    total_usd: float,
    pnl_usd: float,
    source: str = "demo",
) -> None:
    """Persist a trade record and mark the user as having traded."""
    conn = get_connection()
    conn.execute(
        """
        INSERT INTO trades
            (telegram_id, symbol, action, quantity,
             price_usd, total_usd, pnl_usd, source, created_at)
        VALUES (?,?,?,?, ?,?,?,?,?)
        """,
        (telegram_id, symbol, action, quantity,
         price_usd, total_usd, pnl_usd, source, now_iso()),
    )
    # mark user as having done at least one trade
    conn.execute(
        "UPDATE users SET has_traded=1 WHERE telegram_id=?", (telegram_id,)
    )
    conn.commit()
    conn.close()


def record_deposit(telegram_id: int, address: str) -> int:
    """Create a pending deposit request; returns its ID."""
    conn = get_connection()
    cur = conn.execute(
        "INSERT INTO deposits (telegram_id, deposit_address, created_at) "
        "VALUES (?,?,?)",
        (telegram_id, address, now_iso()),
    )
    deposit_id = cur.lastrowid
    conn.commit()
    conn.close()
    return deposit_id


def record_withdrawal(
    telegram_id: int, to_address: str, amount_sol: float, amount_usd: float
) -> int:
    """Create a pending withdrawal request; returns its ID."""
    conn = get_connection()
    cur = conn.execute(
        "INSERT INTO withdrawals "
        "(telegram_id, to_address, amount_sol, amount_usd, created_at) "
        "VALUES (?,?,?,?,?)",
        (telegram_id, to_address, amount_sol, amount_usd, now_iso()),
    )
    wid = cur.lastrowid
    conn.commit()
    conn.close()
    return wid


def get_portfolio_value(telegram_id: int) -> float:
    """Calculate total portfolio value in USD (USDT + coin holdings)."""
    conn = get_connection()
    user = conn.execute(
        "SELECT sol_balance, usdt_balance FROM users WHERE telegram_id=?",
        (telegram_id,),
    ).fetchone()
    holdings = conn.execute(
        "SELECT symbol, quantity FROM holdings WHERE telegram_id=?",
        (telegram_id,),
    ).fetchall()
    conn.close()

    total = (user["sol_balance"] * SOL_PRICE_USD) + user["usdt_balance"]
    for h in holdings:
        sym = h["symbol"]
        if sym in COINS:
            total += h["quantity"] * COINS[sym]["price"]
    return total


# ═══════════════════════════════════════════════════════════════
#  ANTI-SPAM MIDDLEWARE
# ═══════════════════════════════════════════════════════════════

# In-memory rate-limit tracker: {telegram_id: [timestamp, ...]}
_spam_tracker: dict[int, list[float]] = {}


def is_spamming(telegram_id: int) -> bool:
    """
    Return True if the user has exceeded SPAM_LIMIT actions
    within the last SPAM_WINDOW seconds.
    """
    now = time.time()
    history = _spam_tracker.get(telegram_id, [])
    # keep only recent timestamps
    history = [t for t in history if now - t < SPAM_WINDOW]
    history.append(now)
    _spam_tracker[telegram_id] = history
    return len(history) > SPAM_LIMIT


def anti_spam(func):
    """Decorator that blocks spammy users."""
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        tid = update.effective_user.id
        if is_spamming(tid):
            await update.effective_message.reply_text(
                "⚠️ *Slow down!* You're sending too many requests.\n"
                "Please wait a few seconds before trying again.",
                parse_mode=ParseMode.MARKDOWN,
            )
            return
        return await func(update, context)
    return wrapper


# ═══════════════════════════════════════════════════════════════
#  UI HELPERS  (keyboards & formatted messages)
# ═══════════════════════════════════════════════════════════════

def main_menu_keyboard() -> InlineKeyboardMarkup:
    """Build the main menu inline keyboard."""
    buttons = [
        [
            InlineKeyboardButton("💰 Wallet",  callback_data="wallet"),
            InlineKeyboardButton("📈 Trade",   callback_data="trade"),
        ],
        [
            InlineKeyboardButton("📥 Deposit",  callback_data="deposit"),
            InlineKeyboardButton("📤 Withdraw", callback_data="withdraw"),
        ],
        [
            InlineKeyboardButton("📜 History",  callback_data="history"),
            InlineKeyboardButton("👤 Profile",  callback_data="profile"),
        ],
        [
            InlineKeyboardButton("⚙️ Settings", callback_data="settings"),
        ],
    ]
    return InlineKeyboardMarkup(buttons)


def back_keyboard(target: str = "menu") -> InlineKeyboardMarkup:
    """Single back button."""
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("🔙 Back", callback_data=target)]]
    )


def coin_select_keyboard(action: str) -> InlineKeyboardMarkup:
    """Show all supported coins for trading."""
    buttons = []
    row = []
    for i, (sym, info) in enumerate(COINS.items()):
        label = f"{info['emoji']} {sym}"
        row.append(InlineKeyboardButton(label, callback_data=f"coin_{action}_{sym}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton("🔙 Back", callback_data="trade")])
    return InlineKeyboardMarkup(buttons)


def format_float(value: float, decimals: int = 4) -> str:
    """Format a float with comma-thousands and fixed decimals."""
    return f"{value:,.{decimals}f}"


def format_usd(value: float) -> str:
    return f"${value:,.2f}"


def format_change(pct: float) -> str:
    arrow = "🟢 ▲" if pct >= 0 else "🔴 ▼"
    return f"{arrow} {abs(pct):.1f}%"


# ═══════════════════════════════════════════════════════════════
#  COMMAND HANDLERS
# ═══════════════════════════════════════════════════════════════

@anti_spam
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /start — entry point.
    Registers the user on first visit, then shows the main menu.
    """
    tg_user = update.effective_user
    tid     = tg_user.id

    # ── Auto-register if new ──────────────────────────────────
    user = get_user(tid)
    is_new = user is None
    if is_new:
        user = create_user(
            telegram_id=tid,
            username=tg_user.username or "",
            first_name=tg_user.first_name or "Trader",
        )

    update_last_seen(tid)

    name = user["first_name"] or "Trader"

    if is_new:
        welcome = (
            f"🚀 *Welcome to MemeSol Wallet, {name}!*\n\n"
            f"You've been automatically registered.\n\n"
            f"🎁 *Demo Bonus:* {format_usd(DEMO_BONUS_USD)} has been added to your account!\n"
            f"Use it to practice trading Solana meme coins risk-free.\n\n"
            f"📋 *Your Account ID:* `{user['user_uid']}`\n\n"
            f"_Note: Demo bonus is for simulation only and cannot be withdrawn._\n\n"
            f"Choose an option below to get started 👇"
        )
    else:
        welcome = (
            f"👋 *Welcome back, {name}!*\n\n"
            f"📋 *Account ID:* `{user['user_uid']}`\n\n"
            f"What would you like to do today? 👇"
        )

    await update.message.reply_text(
        welcome,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=main_menu_keyboard(),
    )


@anti_spam
async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/menu — show main menu from anywhere."""
    await update.message.reply_text(
        "🏠 *Main Menu*\n\nChoose an option:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=main_menu_keyboard(),
    )


@anti_spam
async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/help — show command reference."""
    help_text = (
        "📖 *MemeSol Wallet — Help*\n\n"
        "*Commands:*\n"
        "/start  — Open / register your wallet\n"
        "/menu   — Show main menu\n"
        "/wallet — View balances\n"
        "/trade  — Open trading desk\n"
        "/deposit — Generate deposit address\n"
        "/help   — This message\n\n"
        "*About:*\n"
        "MemeSol Wallet is a demo trading platform simulating "
        "Solana meme coin markets. All balances are virtual.\n\n"
        "⚠️ _No real funds are involved._"
    )
    await update.message.reply_text(help_text, parse_mode=ParseMode.MARKDOWN)


# ═══════════════════════════════════════════════════════════════
#  CALLBACK QUERY ROUTER
# ═══════════════════════════════════════════════════════════════

@anti_spam
async def callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Central dispatcher for all InlineKeyboard callbacks.
    Reads the callback_data and delegates to the right handler.
    """
    query = update.callback_query
    await query.answer()          # acknowledge the tap

    tid  = query.from_user.id
    data = query.data
    update_last_seen(tid)

    # ── Ensure user exists (edge case) ────────────────────────
    user = get_user(tid)
    if not user:
        await query.edit_message_text(
            "❌ Account not found. Please send /start to register.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    # ── Route ─────────────────────────────────────────────────
    if data == "menu":
        await show_main_menu(query)

    elif data == "wallet":
        await show_wallet(query, user)

    elif data == "trade":
        await show_trade_menu(query)

    elif data == "trade_buy":
        await query.edit_message_text(
            "🛒 *Buy — Select a Coin*\n\nChoose which meme coin to buy:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=coin_select_keyboard("buy"),
        )

    elif data == "trade_sell":
        await query.edit_message_text(
            "💸 *Sell — Select a Coin*\n\nChoose which meme coin to sell:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=coin_select_keyboard("sell"),
        )

    elif data.startswith("coin_buy_") or data.startswith("coin_sell_"):
        parts  = data.split("_")   # ['coin', 'buy'|'sell', 'SYMBOL']
        action = parts[1]
        symbol = parts[2]
        await show_coin_detail(query, user, symbol, action, context)

    elif data.startswith("exec_"):
        await execute_trade(query, user, data, context)

    elif data == "deposit":
        await show_deposit(query, user, context)

    elif data == "withdraw":
        await show_withdraw(query, user, context)

    elif data == "history":
        await show_history(query, tid)

    elif data == "history_trades":
        await show_trade_history(query, tid)

    elif data == "history_deposits":
        await show_deposit_history(query, tid)

    elif data == "history_withdrawals":
        await show_withdrawal_history(query, tid)

    elif data == "profile":
        await show_profile(query, user)

    elif data == "settings":
        await show_settings(query, user)

    elif data == "refresh_prices":
        await show_trade_menu(query)

    else:
        await query.edit_message_text(
            "❓ Unknown action. Tap /menu to go home.",
            reply_markup=back_keyboard("menu"),
        )


# ═══════════════════════════════════════════════════════════════
#  SECTION HANDLERS
# ═══════════════════════════════════════════════════════════════

async def show_main_menu(query) -> None:
    """Re-render the main menu."""
    await query.edit_message_text(
        "🏠 *Main Menu*\n\nChoose an option:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=main_menu_keyboard(),
    )


# ── 💰 WALLET ──────────────────────────────────────────────────

async def show_wallet(query, user: sqlite3.Row) -> None:
    """Display the user's full wallet dashboard."""
    tid       = user["telegram_id"]
    sol_bal   = user["sol_balance"]
    usdt_bal  = user["usdt_balance"]
    demo_bal  = user["demo_bonus"]
    port_val  = get_portfolio_value(tid)

    # Coin holdings summary
    conn = get_connection()
    holdings = conn.execute(
        "SELECT symbol, quantity, avg_buy_price FROM holdings "
        "WHERE telegram_id=? AND quantity > 0",
        (tid,),
    ).fetchall()
    conn.close()

    holdings_text = ""
    for h in holdings:
        sym   = h["symbol"]
        qty   = h["quantity"]
        cur_p = COINS[sym]["price"]
        val   = qty * cur_p
        pnl   = (cur_p - h["avg_buy_price"]) * qty
        pnl_sign = "+" if pnl >= 0 else ""
        emoji = COINS[sym]["emoji"]
        holdings_text += (
            f"  {emoji} *{sym}:* {format_float(qty)} "
            f"≈ {format_usd(val)} "
            f"(PnL: {pnl_sign}{format_usd(pnl)})\n"
        )

    if not holdings_text:
        holdings_text = "  _No coin holdings yet._\n"

    activation = "✅ Activated" if user["is_activated"] else "❌ Not Activated"

    text = (
        f"💰 *Wallet Dashboard*\n"
        f"{'─' * 30}\n"
        f"👤 *User ID:* `{user['user_uid']}`\n"
        f"🔐 *Status:* {activation}\n\n"
        f"*Balances:*\n"
        f"  🔷 SOL:        {format_float(sol_bal, 4)} SOL\n"
        f"  💵 USDT:       {format_usd(usdt_bal)}\n"
        f"  🎁 Demo Bonus: {format_usd(demo_bal)}\n\n"
        f"*Coin Holdings:*\n"
        f"{holdings_text}\n"
        f"*Total Portfolio Value:* {format_usd(port_val)}\n"
        f"{'─' * 30}\n"
        f"_SOL price ref: {format_usd(SOL_PRICE_USD)}_"
    )
    await query.edit_message_text(
        text,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=back_keyboard("menu"),
    )


# ── 📈 TRADE ───────────────────────────────────────────────────

async def show_trade_menu(query) -> None:
    """Show live(ish) coin prices and trade options."""
    price_lines = ""
    for sym, info in COINS.items():
        change_str = format_change(info["change"])
        price_lines += (
            f"  {info['emoji']} *{sym}*  {format_usd(info['price'])}  {change_str}\n"
        )

    text = (
        f"📈 *Trading Desk*\n"
        f"{'─' * 30}\n"
        f"*Market Prices (24h):*\n"
        f"{price_lines}\n"
        f"_Prices are simulated for demo purposes._"
    )
    buttons = [
        [
            InlineKeyboardButton("🟢 Buy",  callback_data="trade_buy"),
            InlineKeyboardButton("🔴 Sell", callback_data="trade_sell"),
        ],
        [
            InlineKeyboardButton("🔄 Refresh Prices", callback_data="refresh_prices"),
        ],
        [InlineKeyboardButton("🔙 Back", callback_data="menu")],
    ]
    await query.edit_message_text(
        text,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def show_coin_detail(
    query,
    user: sqlite3.Row,
    symbol: str,
    action: str,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Show coin detail and preset trade-size buttons."""
    info = COINS[symbol]
    tid  = user["telegram_id"]
    demo = user["demo_bonus"]
    usdt = user["usdt_balance"]

    holding = get_holding(tid, symbol)
    held_qty = holding["quantity"] if holding else 0.0

    change_str = format_change(info["change"])
    action_label = "🟢 BUY" if action == "buy" else "🔴 SELL"

    text = (
        f"{info['emoji']} *{info['name']} ({symbol})*\n"
        f"{'─' * 28}\n"
        f"💲 Price:       {format_usd(info['price'])}\n"
        f"📊 24h Change:  {change_str}\n"
        f"📦 You Hold:    {format_float(held_qty)} {symbol}\n\n"
        f"💵 USDT Balance:  {format_usd(usdt)}\n"
        f"🎁 Demo Bonus:    {format_usd(demo)}\n\n"
        f"*{action_label} — Choose Amount (USD):*"
    )

    # Preset USD amounts
    amounts = [10, 25, 50, 100, 250]
    buttons = []
    row = []
    for amt in amounts:
        row.append(
            InlineKeyboardButton(
                f"${amt}", callback_data=f"exec_{action}_{symbol}_{amt}"
            )
        )
        if len(row) == 3:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)

    buttons.append(
        [InlineKeyboardButton("🔙 Back", callback_data=f"trade_{action}")]
    )

    await query.edit_message_text(
        text,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def execute_trade(
    query,
    user: sqlite3.Row,
    data: str,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """
    Process a buy or sell trade.
    data format: exec_{buy|sell}_{SYMBOL}_{amount_usd}
    """
    _, action, symbol, amt_str = data.split("_", 3)
    amount_usd = float(amt_str)
    tid        = user["telegram_id"]
    price      = COINS[symbol]["price"]
    quantity   = amount_usd / price

    conn = get_connection()

    if action == "buy":
        # ── Fund source: demo bonus first, then USDT ──────────
        demo  = user["demo_bonus"]
        usdt  = user["usdt_balance"]
        funds = demo + usdt

        if funds < amount_usd:
            await query.edit_message_text(
                f"❌ *Insufficient funds!*\n\n"
                f"You need {format_usd(amount_usd)} but only have "
                f"{format_usd(funds)} available.",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=back_keyboard("trade"),
            )
            conn.close()
            return

        # Deduct demo bonus first
        demo_used = min(demo, amount_usd)
        usdt_used = max(0.0, amount_usd - demo_used)
        new_demo  = demo - demo_used
        new_usdt  = usdt - usdt_used

        conn.execute(
            "UPDATE users SET demo_bonus=?, usdt_balance=? WHERE telegram_id=?",
            (new_demo, new_usdt, tid),
        )

        # Update holdings with new weighted avg price
        existing = conn.execute(
            "SELECT quantity, avg_buy_price FROM holdings "
            "WHERE telegram_id=? AND symbol=?",
            (tid, symbol),
        ).fetchone()
        if existing and existing["quantity"] > 0:
            old_qty   = existing["quantity"]
            old_avg   = existing["avg_buy_price"]
            new_qty   = old_qty + quantity
            new_avg   = ((old_qty * old_avg) + (quantity * price)) / new_qty
        else:
            new_qty   = quantity
            new_avg   = price

        conn.execute(
            """
            INSERT INTO holdings (telegram_id, symbol, quantity, avg_buy_price)
            VALUES (?,?,?,?)
            ON CONFLICT(telegram_id, symbol)
            DO UPDATE SET quantity=excluded.quantity,
                          avg_buy_price=excluded.avg_buy_price
            """,
            (tid, symbol, new_qty, new_avg),
        )
        conn.execute(
            "UPDATE users SET has_traded=1 WHERE telegram_id=?", (tid,)
        )
        conn.execute(
            """
            INSERT INTO trades
                (telegram_id, symbol, action, quantity,
                 price_usd, total_usd, pnl_usd, source, created_at)
            VALUES (?,?,?,?, ?,?,0,'demo',?)
            """,
            (tid, symbol, "BUY", quantity, price, amount_usd, now_iso()),
        )
        conn.commit()
        conn.close()

        # Check activation
        _check_and_activate(tid)

        text = (
            f"✅ *Buy Order Executed!*\n"
            f"{'─' * 28}\n"
            f"{COINS[symbol]['emoji']} *{symbol}*\n"
            f"📦 Quantity:  {format_float(quantity)} {symbol}\n"
            f"💲 Price:     {format_usd(price)}\n"
            f"💵 Total:     {format_usd(amount_usd)}\n\n"
            f"🎁 Demo used: {format_usd(demo_used)}\n"
            f"💵 USDT used: {format_usd(usdt_used)}\n\n"
            f"_Your holding has been updated._"
        )

    else:  # sell
        holding = conn.execute(
            "SELECT quantity, avg_buy_price FROM holdings "
            "WHERE telegram_id=? AND symbol=?",
            (tid, symbol),
        ).fetchone()

        if not holding or holding["quantity"] < quantity:
            held = holding["quantity"] if holding else 0.0
            await query.edit_message_text(
                f"❌ *Insufficient {symbol}!*\n\n"
                f"You want to sell {format_float(quantity)} {symbol} "
                f"but only hold {format_float(held)}.",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=back_keyboard("trade"),
            )
            conn.close()
            return

        old_qty = holding["quantity"]
        avg_buy = holding["avg_buy_price"]
        pnl     = (price - avg_buy) * quantity
        new_qty = old_qty - quantity

        conn.execute(
            "UPDATE holdings SET quantity=? WHERE telegram_id=? AND symbol=?",
            (new_qty, tid, symbol),
        )
        conn.execute(
            "UPDATE users SET usdt_balance = usdt_balance + ?, has_traded=1 "
            "WHERE telegram_id=?",
            (amount_usd, tid),
        )
        conn.execute(
            """
            INSERT INTO trades
                (telegram_id, symbol, action, quantity,
                 price_usd, total_usd, pnl_usd, source, created_at)
            VALUES (?,?,?,?, ?,?,?,'demo',?)
            """,
            (tid, symbol, "SELL", quantity, price, amount_usd, pnl, now_iso()),
        )
        conn.commit()
        conn.close()

        pnl_sign  = "+" if pnl >= 0 else ""
        pnl_emoji = "🟢" if pnl >= 0 else "🔴"

        text = (
            f"✅ *Sell Order Executed!*\n"
            f"{'─' * 28}\n"
            f"{COINS[symbol]['emoji']} *{symbol}*\n"
            f"📦 Quantity:  {format_float(quantity)} {symbol}\n"
            f"💲 Price:     {format_usd(price)}\n"
            f"💵 Received:  {format_usd(amount_usd)}\n"
            f"{pnl_emoji} PnL:      {pnl_sign}{format_usd(pnl)}\n\n"
            f"_USDT balance updated._"
        )

    await query.edit_message_text(
        text,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("📈 Trade More", callback_data="trade")],
            [InlineKeyboardButton("💰 Wallet",     callback_data="wallet")],
            [InlineKeyboardButton("🏠 Menu",       callback_data="menu")],
        ]),
    )


def _check_and_activate(telegram_id: int) -> None:
    """
    Automatically activate the account if both conditions are met:
    1. total_deposited >= MIN_ACTIVATION_USD
    2. has_traded == 1
    """
    conn = get_connection()
    user = conn.execute(
        "SELECT is_activated, has_traded, total_deposited FROM users "
        "WHERE telegram_id=?",
        (telegram_id,),
    ).fetchone()
    if (
        user
        and not user["is_activated"]
        and user["has_traded"]
        and user["total_deposited"] >= MIN_ACTIVATION_USD
    ):
        conn.execute(
            "UPDATE users SET is_activated=1 WHERE telegram_id=?",
            (telegram_id,),
        )
        conn.commit()
    conn.close()


# ── 📥 DEPOSIT ─────────────────────────────────────────────────

async def show_deposit(query, user: sqlite3.Row, context) -> None:
    """Generate a deposit address and display instructions."""
    tid     = user["telegram_id"]
    address = generate_sol_address()
    record_deposit(tid, address)

    text = (
        f"📥 *Deposit SOL*\n"
        f"{'─' * 30}\n"
        f"Send *SOL* to the address below to fund your account.\n\n"
        f"🔐 *Your Deposit Address:*\n"
        f"`{address}`\n\n"
        f"📌 *Notes:*\n"
        f"• Minimum activation deposit: {format_usd(MIN_ACTIVATION_USD)}\n"
        f"• Only send SOL on the *Solana* network\n"
        f"• Deposits are credited after confirmation\n"
        f"• Address is valid for this session\n\n"
        f"⚠️ _This is a simulated demo platform._\n"
        f"_Do NOT send real funds._"
    )
    buttons = [
        [InlineKeyboardButton("📜 Deposit History", callback_data="history_deposits")],
        [InlineKeyboardButton("🔙 Back",            callback_data="menu")],
    ]
    await query.edit_message_text(
        text,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(buttons),
    )


# ── 📤 WITHDRAW ────────────────────────────────────────────────

async def show_withdraw(query, user: sqlite3.Row, context) -> None:
    """
    Check withdrawal eligibility and either show the form
    or display a descriptive warning.
    """
    tid = user["telegram_id"]

    # ── Eligibility checks ────────────────────────────────────
    reasons = []
    if not user["is_activated"]:
        reasons.append("❌ Account not activated")
    if user["total_deposited"] < MIN_ACTIVATION_USD:
        reasons.append(
            f"❌ Minimum deposit of {format_usd(MIN_ACTIVATION_USD)} not met "
            f"(deposited: {format_usd(user['total_deposited'])})"
        )
    if not user["has_traded"]:
        reasons.append("❌ You must complete at least one trade first")

    if reasons:
        reason_text = "\n".join(reasons)
        text = (
            f"📤 *Withdrawal*\n"
            f"{'─' * 30}\n"
            f"⚠️ *You cannot withdraw yet.*\n\n"
            f"*Reasons:*\n{reason_text}\n\n"
            f"*To unlock withdrawals:*\n"
            f"1️⃣ Deposit at least {format_usd(MIN_ACTIVATION_USD)} in SOL\n"
            f"2️⃣ Complete at least one trade\n\n"
            f"_Your account will be activated automatically "
            f"once both conditions are met._"
        )
        await query.edit_message_text(
            text,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=back_keyboard("menu"),
        )
        return

    # ── Eligible — show balance & instructions ─────────────────
    sol_bal = user["sol_balance"]
    text = (
        f"📤 *Withdraw SOL*\n"
        f"{'─' * 30}\n"
        f"🔷 Available SOL: {format_float(sol_bal)}\n"
        f"💵 ≈ {format_usd(sol_bal * SOL_PRICE_USD)}\n\n"
        f"✅ Your account is activated.\n\n"
        f"To request a withdrawal, please contact support "
        f"or use the withdrawal form.\n\n"
        f"_Minimum withdrawal: 0.01 SOL_\n"
        f"⚠️ _Demo platform — no real funds involved._"
    )
    buttons = [
        [InlineKeyboardButton("📜 Withdrawal History", callback_data="history_withdrawals")],
        [InlineKeyboardButton("🔙 Back", callback_data="menu")],
    ]
    await query.edit_message_text(
        text,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(buttons),
    )


# ── 📜 HISTORY ─────────────────────────────────────────────────

async def show_history(query, tid: int) -> None:
    """History sub-menu."""
    text = "📜 *Transaction History*\n\nChoose a history type:"
    buttons = [
        [InlineKeyboardButton("📈 Trade History",      callback_data="history_trades")],
        [InlineKeyboardButton("📥 Deposit History",    callback_data="history_deposits")],
        [InlineKeyboardButton("📤 Withdrawal History", callback_data="history_withdrawals")],
        [InlineKeyboardButton("🔙 Back",               callback_data="menu")],
    ]
    await query.edit_message_text(
        text,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def show_trade_history(query, tid: int) -> None:
    """Show last 10 trades."""
    conn = get_connection()
    trades = conn.execute(
        "SELECT * FROM trades WHERE telegram_id=? ORDER BY id DESC LIMIT 10",
        (tid,),
    ).fetchall()
    conn.close()

    if not trades:
        text = "📈 *Trade History*\n\n_No trades yet. Start trading to see history!_"
    else:
        lines = "📈 *Recent Trades (last 10)*\n" + "─" * 30 + "\n"
        for t in trades:
            action_icon = "🟢" if t["action"] == "BUY" else "🔴"
            pnl = t["pnl_usd"]
            pnl_str = ""
            if t["action"] == "SELL":
                sign = "+" if pnl >= 0 else ""
                pnl_str = f" | PnL: {sign}{format_usd(pnl)}"
            lines += (
                f"{action_icon} *{t['action']} {t['symbol']}*\n"
                f"   {format_float(t['quantity'])} @ {format_usd(t['price_usd'])}"
                f" = {format_usd(t['total_usd'])}{pnl_str}\n"
                f"   🕐 {t['created_at'][:16]}\n\n"
            )
        text = lines

    await query.edit_message_text(
        text,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=back_keyboard("history"),
    )


async def show_deposit_history(query, tid: int) -> None:
    """Show last 10 deposit requests."""
    conn = get_connection()
    deps = conn.execute(
        "SELECT * FROM deposits WHERE telegram_id=? ORDER BY id DESC LIMIT 10",
        (tid,),
    ).fetchall()
    conn.close()

    if not deps:
        text = "📥 *Deposit History*\n\n_No deposits recorded yet._"
    else:
        lines = "📥 *Deposit History (last 10)*\n" + "─" * 30 + "\n"
        for d in deps:
            status_icon = "⏳" if d["status"] == "pending" else "✅"
            addr_short  = d["deposit_address"][:12] + "…"
            lines += (
                f"{status_icon} *{d['status'].upper()}*\n"
                f"   Address: `{addr_short}`\n"
                f"   🕐 {d['created_at'][:16]}\n\n"
            )
        text = lines

    await query.edit_message_text(
        text,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=back_keyboard("history"),
    )


async def show_withdrawal_history(query, tid: int) -> None:
    """Show last 10 withdrawal requests."""
    conn = get_connection()
    wds = conn.execute(
        "SELECT * FROM withdrawals WHERE telegram_id=? ORDER BY id DESC LIMIT 10",
        (tid,),
    ).fetchall()
    conn.close()

    if not wds:
        text = "📤 *Withdrawal History*\n\n_No withdrawals recorded yet._"
    else:
        lines = "📤 *Withdrawal History (last 10)*\n" + "─" * 30 + "\n"
        for w in wds:
            status_icon = "⏳" if w["status"] == "pending" else "✅"
            addr_short  = w["to_address"][:12] + "…"
            lines += (
                f"{status_icon} *{w['status'].upper()}*\n"
                f"   To: `{addr_short}`\n"
                f"   Amount: {format_float(w['amount_sol'])} SOL "
                f"≈ {format_usd(w['amount_usd'])}\n"
                f"   🕐 {w['created_at'][:16]}\n\n"
            )
        text = lines

    await query.edit_message_text(
        text,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=back_keyboard("history"),
    )


# ── 👤 PROFILE ─────────────────────────────────────────────────

async def show_profile(query, user: sqlite3.Row) -> None:
    """Display user profile and account stats."""
    tid = user["telegram_id"]

    conn = get_connection()
    trade_count   = conn.execute(
        "SELECT COUNT(*) AS c FROM trades WHERE telegram_id=?", (tid,)
    ).fetchone()["c"]
    deposit_count = conn.execute(
        "SELECT COUNT(*) AS c FROM deposits WHERE telegram_id=?", (tid,)
    ).fetchone()["c"]
    total_pnl     = conn.execute(
        "SELECT COALESCE(SUM(pnl_usd),0) AS p FROM trades "
        "WHERE telegram_id=? AND action='SELL'",
        (tid,),
    ).fetchone()["p"]
    conn.close()

    activated = "✅ Activated" if user["is_activated"] else "❌ Not Activated"
    pnl_sign  = "+" if total_pnl >= 0 else ""
    pnl_emoji = "🟢" if total_pnl >= 0 else "🔴"

    text = (
        f"👤 *User Profile*\n"
        f"{'─' * 30}\n"
        f"🆔 *User ID:*     `{user['user_uid']}`\n"
        f"👤 *Username:*    @{user['username'] or 'N/A'}\n"
        f"📛 *Name:*        {user['first_name']}\n"
        f"🔐 *Status:*      {activated}\n\n"
        f"*Statistics:*\n"
        f"📈 Total Trades:  {trade_count}\n"
        f"📥 Deposits:      {deposit_count}\n"
        f"💰 Deposited:     {format_usd(user['total_deposited'])}\n"
        f"{pnl_emoji} Total PnL:    {pnl_sign}{format_usd(total_pnl)}\n\n"
        f"*Account Created:*\n"
        f"🕐 {user['created_at'][:16]} UTC\n"
        f"👁 Last Seen: {user['last_seen'][:16]} UTC"
    )
    await query.edit_message_text(
        text,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=back_keyboard("menu"),
    )


# ── ⚙️ SETTINGS ────────────────────────────────────────────────

async def show_settings(query, user: sqlite3.Row) -> None:
    """Settings panel."""
    activation = "✅ Active" if user["is_activated"] else "❌ Inactive"
    text = (
        f"⚙️ *Settings*\n"
        f"{'─' * 30}\n"
        f"🆔 *User ID:*    `{user['user_uid']}`\n"
        f"🔐 *Account:*   {activation}\n\n"
        f"*Activation Requirements:*\n"
        f"{'✅' if user['total_deposited'] >= MIN_ACTIVATION_USD else '❌'} "
        f"Deposit ≥ {format_usd(MIN_ACTIVATION_USD)} "
        f"(yours: {format_usd(user['total_deposited'])})\n"
        f"{'✅' if user['has_traded'] else '❌'} At least 1 trade completed\n\n"
        f"_Contact @support for assistance._"
    )
    await query.edit_message_text(
        text,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=back_keyboard("menu"),
    )


# ═══════════════════════════════════════════════════════════════
#  FALLBACK / ERROR HANDLERS
# ═══════════════════════════════════════════════════════════════

async def handle_unknown_text(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Catch-all for plain text messages."""
    await update.message.reply_text(
        "🤖 Use the menu buttons or type /menu to navigate.",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("🏠 Main Menu", callback_data="menu")]]
        ),
    )


async def handle_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log errors and notify the user gracefully."""
    logger.error("Unhandled exception: %s", context.error, exc_info=True)
    if isinstance(update, Update) and update.effective_message:
        await update.effective_message.reply_text(
            "⚠️ Something went wrong on our end. Please try again or type /start.",
        )


# ═══════════════════════════════════════════════════════════════
#  MAIN — Application Bootstrap
# ═══════════════════════════════════════════════════════════════

def main() -> None:
    """
    Initialise the database, build the Application, register all
    handlers, and start polling.
    """
    # ── Database setup ────────────────────────────────────────
    init_db()

    # ── Build Application ─────────────────────────────────────
    app = Application.builder().token(BOT_TOKEN).build()

    # ── Command handlers ──────────────────────────────────────
    app.add_handler(CommandHandler("start",  cmd_start))
    app.add_handler(CommandHandler("menu",   cmd_menu))
    app.add_handler(CommandHandler("help",   cmd_help))
    app.add_handler(CommandHandler("wallet", lambda u, c: callback_router(u, c)))

    # ── Callback query handler (all inline buttons) ───────────
    app.add_handler(CallbackQueryHandler(callback_router))

    # ── Fallback for plain text ───────────────────────────────
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_unknown_text)
    )

    # ── Error handler ─────────────────────────────────────────
    app.add_error_handler(handle_error)

    # ── Start polling ─────────────────────────────────────────
    logger.info("MemeSol Wallet Bot is running…  Press Ctrl+C to stop.")
    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )


# ─────────────────────────────────────────────
if __name__ == "__main__":
    main()
