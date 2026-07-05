import os
import json
import logging
import asyncio
import time
import threading
import random
import requests
from datetime import datetime
from typing import Optional, Dict, Callable
from enum import Enum

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
    ContextTypes,
)
from flask import Flask, request

try:
    from waitress import serve as waitress_serve
    WAITRESS_AVAILABLE = True
except ImportError:
    WAITRESS_AVAILABLE = False

try:
    import psycopg2
    from psycopg2.extras import Json as PgJson
    PSYCOPG2_AVAILABLE = True
except ImportError:
    PSYCOPG2_AVAILABLE = False

# ===================== LOGGING =====================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)
logging.getLogger('httpx').setLevel(logging.WARNING)
logging.getLogger('httpcore').setLevel(logging.WARNING)
logging.getLogger('urllib3').setLevel(logging.WARNING)
logging.getLogger('waitress').setLevel(logging.WARNING)

# ===================== CONFIG =====================
TOKEN = os.getenv('TELEGRAM_TOKEN')
WEBHOOK_URL = os.getenv('WEBHOOK_URL', '').strip()
PORT = int(os.getenv('PORT', 8000))
ADMIN_IDS = [int(x) for x in os.getenv('ADMIN_IDS', '').split(',') if x.strip()]
DATA_DIR = os.getenv('DATA_DIR', '.').strip() or '.'
DATA_FILE = os.path.join(DATA_DIR, 'arcade_users.json')
DATABASE_URL = os.getenv('DATABASE_URL', '').strip()

MIN_BET = 1
MAX_BET = 100_000
DAILY_BONUS = 100
NATIVE_ANIMATION_WAIT = 4  # seconds to let the Telegram dice/darts/etc animation play

if not TOKEN or not WEBHOOK_URL:
    raise ValueError("Missing TELEGRAM_TOKEN or WEBHOOK_URL")

logger.info(f"Bot configured - Webhook: {WEBHOOK_URL}")

# ===================== FLASK / GLOBALS =====================
flask_app = Flask(__name__)
bot_app: Optional[Application] = None
bot_loop: Optional[asyncio.AbstractEventLoop] = None

_dup_lock = threading.Lock()
_processed_ids = set()

def already_processed(update_id: int) -> bool:
    with _dup_lock:
        if update_id in _processed_ids:
            return True
        _processed_ids.add(update_id)
        if len(_processed_ids) > 5000:
            _processed_ids.clear()
        return False

# ===================== ENUMS =====================
class VIPLevel(int, Enum):
    BRONZE = 0
    SILVER = 1
    GOLD = 2
    PLATINUM = 3
    DIAMOND = 4

VIP_NAMES = {
    VIPLevel.BRONZE: "Bronze", VIPLevel.SILVER: "Silver", VIPLevel.GOLD: "Gold",
    VIPLevel.PLATINUM: "Platinum", VIPLevel.DIAMOND: "Diamond"
}
VIP_RAKEBACK = {
    VIPLevel.BRONZE: 0, VIPLevel.SILVER: 0.01, VIPLevel.GOLD: 0.02,
    VIPLevel.PLATINUM: 0.03, VIPLevel.DIAMOND: 0.05
}

def get_vip_level(xp: int) -> VIPLevel:
    if xp >= 50000: return VIPLevel.DIAMOND
    if xp >= 15000: return VIPLevel.PLATINUM
    if xp >= 5000: return VIPLevel.GOLD
    if xp >= 1000: return VIPLevel.SILVER
    return VIPLevel.BRONZE

# ===================== EXCEPTIONS =====================
class InsufficientFunds(Exception):
    pass

class AlreadyClaimed(Exception):
    pass

# ===================== DEFAULT USER =====================

def default_user(user_id: int) -> dict:
    return {
        'username': '',
        'coins': 1000,
        'xp': 0,
        'ref_code': f"TG{user_id % 100000:05d}",
        'referrer': None,
        'referrals': [],
        'wallet': {'total_deposited': 0, 'total_withdrawn': 0},
        'stats': {
            'games_played': 0, 'total_wagered': 0, 'total_won': 0,
            'biggest_win': 0, 'game_history': {}, 'last_daily': None
        },
        'created_at': datetime.now().isoformat(),
    }

# ===================== STORAGE BACKENDS =====================
# Both backends implement: get_all_users() -> dict, update_user(uid, default_factory, mutate_fn) -> dict (atomic)

class JSONStorage:
    def __init__(self, path):
        self.path = path
        self._lock = threading.Lock()
        os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
        if not os.path.exists(path):
            with open(path, 'w') as f:
                json.dump({}, f)

    def _load(self) -> dict:
        try:
            with open(self.path, 'r') as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"JSONStorage load error: {e}")
            return {}

    def _save(self, data: dict) -> None:
        tmp = self.path + '.tmp'
        with open(tmp, 'w') as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, self.path)  # atomic on POSIX

    def get_all_users(self) -> dict:
        with self._lock:
            return self._load()

    def update_user(self, user_id: str, default_factory: Callable[[], dict], mutate_fn: Callable[[dict], None]) -> dict:
        with self._lock:
            data = self._load()
            if user_id not in data:
                data[user_id] = default_factory()
            mutate_fn(data[user_id])  # may raise; if so nothing is saved
            self._save(data)
            return data[user_id]


class PostgresStorage:
    def __init__(self, url):
        self.url = url
        self._init_schema()

    def _connect(self):
        return psycopg2.connect(self.url)

    def _init_schema(self):
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS arcade_users (
                        user_id TEXT PRIMARY KEY,
                        data JSONB NOT NULL
                    )
                """)
            conn.commit()
        finally:
            conn.close()

    def get_all_users(self) -> dict:
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT user_id, data FROM arcade_users")
                rows = cur.fetchall()
            return {uid: data for uid, data in rows}
        finally:
            conn.close()

    def update_user(self, user_id: str, default_factory: Callable[[], dict], mutate_fn: Callable[[dict], None]) -> dict:
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT data FROM arcade_users WHERE user_id = %s FOR UPDATE", (user_id,))
                row = cur.fetchone()
                if row is None:
                    data = default_factory()
                    cur.execute(
                        "INSERT INTO arcade_users (user_id, data) VALUES (%s, %s)",
                        (user_id, PgJson(data))
                    )
                else:
                    data = row[0]
                mutate_fn(data)  # may raise -> rollback below, no partial write
                cur.execute(
                    "UPDATE arcade_users SET data = %s WHERE user_id = %s",
                    (PgJson(data), user_id)
                )
            conn.commit()
            return data
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


if DATABASE_URL and PSYCOPG2_AVAILABLE:
    storage = PostgresStorage(DATABASE_URL)
    logger.info("Storage backend: Postgres (persistent across restarts)")
elif DATABASE_URL and not PSYCOPG2_AVAILABLE:
    logger.error("DATABASE_URL set but psycopg2 not installed - falling back to local JSON storage")
    storage = JSONStorage(DATA_FILE)
else:
    storage = JSONStorage(DATA_FILE)
    logger.info(f"Storage backend: local JSON at {DATA_FILE} (resets on restart unless DATA_DIR is a persistent disk)")

async def user_update(user_id: int, mutate_fn: Callable[[dict], None]) -> dict:
    uid = str(user_id)
    return await asyncio.to_thread(storage.update_user, uid, lambda: default_user(user_id), mutate_fn)

async def get_user(user_id: int) -> dict:
    return await user_update(user_id, lambda u: None)

async def users_snapshot() -> dict:
    return await asyncio.to_thread(storage.get_all_users)

# ===================== MUTATION FACTORIES =====================

def make_deduct(bet: int):
    def fn(u):
        if u['coins'] < bet:
            raise InsufficientFunds()
        u['coins'] -= bet
    return fn

def make_refund(amount: int):
    def fn(u):
        u['coins'] += amount
    return fn

def make_apply_result(bet: int, mult: float, game_key: str, out: dict):
    def fn(u):
        payout = int(bet * mult)
        u['coins'] += payout
        if payout > 0:
            u['stats']['total_won'] += payout
            if payout > u['stats']['biggest_win']:
                u['stats']['biggest_win'] = payout
        u['stats']['games_played'] += 1
        u['stats']['total_wagered'] += bet
        u['xp'] += max(1, bet // 10)
        u['stats']['game_history'][game_key] = u['stats']['game_history'].get(game_key, 0) + 1

        vip = get_vip_level(u['xp'])
        rakeback = int(bet * VIP_RAKEBACK[vip])
        if rakeback > 0:
            u['coins'] += rakeback

        out['payout'] = payout
        out['rakeback'] = rakeback
    return fn

def make_daily_claim():
    def fn(u):
        today = datetime.now().date().isoformat()
        if u['stats'].get('last_daily') == today:
            raise AlreadyClaimed()
        u['stats']['last_daily'] = today
        u['coins'] += DAILY_BONUS
    return fn

def make_deposit(amount: int):
    def fn(u):
        u['coins'] += amount
        u['wallet']['total_deposited'] += amount
    return fn

def make_withdraw(amount: int):
    def fn(u):
        if u['coins'] < amount:
            raise InsufficientFunds()
        u['coins'] -= amount
        u['wallet']['total_withdrawn'] += amount
    return fn

def make_set_username(name: str):
    def fn(u):
        u['username'] = name
    return fn

def make_mark_referred(referrer_id: str, bonus: int, out: dict):
    def fn(u):
        if u.get('referrer'):
            out['applied'] = False
            return
        u['referrer'] = referrer_id
        u['coins'] += bonus
        out['applied'] = True
    return fn

def make_add_referral(new_user_id: str, bonus: int):
    def fn(u):
        u.setdefault('referrals', [])
        if new_user_id not in u['referrals']:
            u['referrals'].append(new_user_id)
            u['coins'] += bonus
    return fn

# ===================== NATIVE TELEGRAM GAMES =====================

def eval_dice(v):       # 🎲 values 1-6
    if v == 6: return 5.0
    if v in (4, 5): return 2.0
    return 0.0

def eval_darts(v):      # 🎯 values 1-6 (6 = bullseye)
    if v == 6: return 10.0
    if v in (4, 5): return 2.0
    return 0.0

def eval_basketball(v):  # 🏀 values 1-5 (4,5 = basket in)
    if v == 5: return 3.0
    if v == 4: return 1.5
    return 0.0

def eval_football(v):   # ⚽ values 1-5 (3,4,5 = goal)
    if v == 5: return 3.0
    if v in (3, 4): return 1.5
    return 0.0

def eval_bowling(v):    # 🎳 values 1-6 (6 = strike)
    if v == 6: return 10.0
    if v in (4, 5): return 2.0
    if v in (2, 3): return 0.5
    return 0.0

def eval_slots(v):      # 🎰 values 1-64 (1,22,43,64 = three-of-a-kind)
    if v == 64: return 50.0
    if v in (1, 22, 43): return 20.0
    return 0.0

NATIVE_GAMES = {
    'dice':       {'emoji': '🎲', 'name': 'Dice',       'eval': eval_dice},
    'darts':      {'emoji': '🎯', 'name': 'Darts',      'eval': eval_darts},
    'basketball': {'emoji': '🏀', 'name': 'Basketball', 'eval': eval_basketball},
    'football':   {'emoji': '⚽', 'name': 'Football',   'eval': eval_football},
    'bowling':    {'emoji': '🎳', 'name': 'Bowling',    'eval': eval_bowling},
    'slots':      {'emoji': '🎰', 'name': 'Slots',      'eval': eval_slots},
}

# ===================== CUSTOM GAME ENGINE =====================

class GameEngine:
    @staticmethod
    def mines(bombs: int = 5) -> float:
        safe_tiles = random.randint(1, min(8, 25 - bombs))
        mult = 1.0
        for i in range(safe_tiles):
            mult *= (25 - i) / (25 - i - bombs)
        mult *= 0.95
        hit_bomb = random.random() < (safe_tiles / (25 - bombs))
        return 0.0 if hit_bomb else round(mult, 2)

    @staticmethod
    def keno():
        drawn = set(random.sample(range(1, 41), 10))
        picks = set(random.sample(range(1, 41), 5))
        matches = len(picks & drawn)
        table = {0: 0, 1: 0, 2: 0.5, 3: 1.5, 4: 4, 5: 15}
        return table.get(matches, 0), matches

    @staticmethod
    def coinflip(choice: str):
        result = random.choice(['heads', 'tails'])
        won = (result == choice)
        return (2.0 if won else 0.0), result

    @staticmethod
    def crash(cashout_target: float = 2.0):
        crash_point = round(random.uniform(1.0, 8.0), 2)
        won = cashout_target <= crash_point
        return (cashout_target if won else 0.0), crash_point

# ===================== UI HELPERS =====================

async def safe_edit(query, text, reply_markup=None, parse_mode='Markdown'):
    try:
        await query.edit_message_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
    except BadRequest as e:
        if 'Message is not modified' not in str(e):
            raise

def summary_text(bet: int, mult: float, payout: int, rakeback: int, roll_line: str = "") -> str:
    won = mult > 0
    lines = []
    if roll_line:
        lines.append(roll_line)
    if won:
        net = payout - bet
        lines.append(f"✅ *WIN*  |  Multiplier: `x{mult}`")
        lines.append(f"Staked: `{bet}`  →  Returned: `{payout}`  (+{net})")
    else:
        lines.append(f"❌ *LOSS*  |  Multiplier: `x0`")
        lines.append(f"Staked: `{bet}`  →  Lost: `-{bet}`")
    if rakeback > 0:
        lines.append(f"🎁 VIP Rakeback: +{rakeback}")
    return "\n".join(lines)

def main_menu_keyboard(user_id: int):
    keyboard = [
        [InlineKeyboardButton("🎮 Play Games", callback_data='games_menu')],
        [InlineKeyboardButton("💰 Wallet", callback_data='wallet'), InlineKeyboardButton("📊 Stats", callback_data='stats')],
        [InlineKeyboardButton("🏆 Leaderboard", callback_data='leaderboard'), InlineKeyboardButton("🎁 Referral", callback_data='referral')],
        [InlineKeyboardButton("👤 Profile", callback_data='profile'), InlineKeyboardButton("🎉 Daily Bonus", callback_data='daily_bonus')],
    ]
    if user_id in ADMIN_IDS:
        keyboard.append([InlineKeyboardButton("🔧 Admin Panel", callback_data='admin_panel')])
    return InlineKeyboardMarkup(keyboard)

def main_menu_text(user: dict) -> str:
    vip = get_vip_level(user['xp'])
    return (
        f"🎰 *TokenArcade*\n\n"
        f"💰 Balance: `{user['coins']}`\n"
        f"⭐ VIP: {VIP_NAMES[vip]}\n\n"
        f"Choose an option:"
    )

def result_keyboard(replay_target: str):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Play Again", callback_data=replay_target)],
        [InlineKeyboardButton("🎮 Other Games", callback_data='games_menu')],
        [InlineKeyboardButton("🏠 Main Menu", callback_data='back_to_main')],
    ])

GAMES_MENU_KEYBOARD = InlineKeyboardMarkup([
    [InlineKeyboardButton("🎲 Dice", callback_data='bet_dice'), InlineKeyboardButton("🎯 Darts", callback_data='bet_darts')],
    [InlineKeyboardButton("🏀 Basketball", callback_data='bet_basketball'), InlineKeyboardButton("⚽ Football", callback_data='bet_football')],
    [InlineKeyboardButton("🎳 Bowling", callback_data='bet_bowling'), InlineKeyboardButton("🎰 Slots", callback_data='bet_slots')],
    [InlineKeyboardButton("💣 Mines", callback_data='bet_mines'), InlineKeyboardButton("🎱 Keno", callback_data='bet_keno')],
    [InlineKeyboardButton("🪙 Coinflip", callback_data='bet_coinflip'), InlineKeyboardButton("📈 Crash", callback_data='bet_crash')],
    [InlineKeyboardButton("🔙 Back", callback_data='back_to_main')],
])

GAME_DESCRIPTIONS = {
    'dice': "🎲 *Dice*\nTelegram rolls the die. 6 = 5x, 4-5 = 2x, else lose.",
    'darts': "🎯 *Darts*\nTelegram throws the dart. Bullseye (6) = 10x, 4-5 = 2x, else lose.",
    'basketball': "🏀 *Basketball*\nTelegram takes the shot. Swish (5) = 3x, In (4) = 1.5x, else lose.",
    'football': "⚽ *Football*\nTelegram takes the shot. Top corner (5) = 3x, Goal (3-4) = 1.5x, else lose.",
    'bowling': "🎳 *Bowling*\nTelegram rolls the ball. Strike (6) = 10x, 4-5 = 2x, 2-3 = 0.5x, else lose.",
    'slots': "🎰 *Slots*\nTelegram spins the reels. Jackpot 777 = 50x, other triples = 20x, else lose.",
    'mines': "💣 *Mines*\nDodge the bombs. Multiplier grows the further you survive.",
    'keno': "🎱 *Keno*\n5 numbers vs 10 drawn. More matches = bigger multiplier.",
    'coinflip': "🪙 *Coinflip*\nCall it in the air. 2x on a correct call.",
    'crash': "📈 *Crash*\nAuto cash-out at 2x if the crash point is higher.",
}

# ===================== HANDLERS: CORE =====================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.info(f"/start from {update.effective_user.first_name} ({update.effective_user.id})")
    context.user_data.clear()
    user_id = update.effective_user.id
    username = update.effective_user.first_name or 'Player'

    user = await user_update(user_id, make_set_username(username))

    if context.args and not user.get('referrer'):
        ref_code = context.args[0].strip()
        users = await users_snapshot()
        referrer_id = None
        for uid, u in users.items():
            if u.get('ref_code') == ref_code and uid != str(user_id):
                referrer_id = uid
                break
        if referrer_id:
            out = {}
            user = await user_update(user_id, make_mark_referred(referrer_id, 150, out))
            if out.get('applied'):
                await user_update(referrer_id, make_add_referral(str(user_id), 150))

    await update.message.reply_text(
        f"🎰 *Welcome to TokenArcade!*\n\n"
        f"Hi {user['username']}!\n\n"
        f"💰 Balance: `{user['coins']}`\n"
        f"🎁 Your Referral Code: `{user['ref_code']}`\n\n"
        f"Choose an option below:",
        reply_markup=main_menu_keyboard(user_id),
        parse_mode='Markdown'
    )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "*TokenArcade Help*\n\n"
        "/start - Open the main menu\n"
        "/daily - Claim your daily bonus\n"
        "/cancel - Cancel whatever you're doing\n\n"
        "To play: Play Games → pick a game → type your bet amount.",
        parse_mode='Markdown'
    )

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.clear()
    await update.message.reply_text("Cancelled. Send /start to go back to the menu.")

async def daily_bonus(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    try:
        user = await user_update(user_id, make_daily_claim())
    except AlreadyClaimed:
        msg = "❌ You already claimed your daily bonus today. Come back tomorrow!"
        if update.callback_query:
            await update.callback_query.answer(msg, show_alert=True)
        else:
            await update.message.reply_text(msg)
        return

    text = f"✅ Daily bonus claimed! +{DAILY_BONUS} coins.\n\nNew balance: `{user['coins']}`"
    if update.callback_query:
        await update.callback_query.answer("Bonus claimed!")
        await safe_edit(
            update.callback_query, text,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data='back_to_main')]])
        )
    else:
        await update.message.reply_text(text, parse_mode='Markdown')

# ===================== GAMES MENU / BET PROMPT =====================

async def games_menu(update, context):
    query = update.callback_query
    await query.answer()
    context.user_data.clear()
    await safe_edit(query, "🎮 *Choose a Game*", reply_markup=GAMES_MENU_KEYBOARD)

async def back_to_main(update, context):
    query = update.callback_query
    await query.answer()
    context.user_data.clear()
    user = await get_user(query.from_user.id)
    await safe_edit(query, main_menu_text(user), reply_markup=main_menu_keyboard(query.from_user.id))

async def prompt_bet(update, context, game_key: str):
    query = update.callback_query
    await query.answer()
    user = await get_user(query.from_user.id)

    context.user_data.clear()
    context.user_data['pending_game'] = game_key

    text = (
        f"{GAME_DESCRIPTIONS.get(game_key, '')}\n\n"
        f"💰 Your balance: `{user['coins']}`\n\n"
        f"⌨️ *Type your bet amount now* (between {MIN_BET} and {MAX_BET}), or /cancel."
    )
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data='games_menu')]])
    await safe_edit(query, text, reply_markup=keyboard)

# ===================== TEXT MESSAGE HANDLER =====================

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (update.message.text or '').strip()

    pending_wallet = context.user_data.get('pending_wallet')
    if pending_wallet:
        await handle_wallet_text(update, context, text, pending_wallet)
        return

    if context.user_data.get('coinflip_bet') is not None:
        await update.message.reply_text("Please pick Heads or Tails using the buttons above, or /cancel.")
        return

    pending_game = context.user_data.get('pending_game')
    if not pending_game:
        await update.message.reply_text("Send /start to open the menu.")
        return

    if not text.isdigit():
        await update.message.reply_text("❌ Please type a valid whole number for your bet, e.g. `100`", parse_mode='Markdown')
        return

    bet = int(text)
    if bet < MIN_BET or bet > MAX_BET:
        await update.message.reply_text(f"❌ Bet must be between {MIN_BET} and {MAX_BET}.")
        return

    if pending_game in NATIVE_GAMES:
        context.user_data.pop('pending_game', None)
        await play_native_game(update, context, pending_game, bet)
        return

    if pending_game == 'coinflip':
        context.user_data['coinflip_bet'] = bet
        context.user_data.pop('pending_game', None)
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🪙 Heads", callback_data='flip_heads'),
             InlineKeyboardButton("🪙 Tails", callback_data='flip_tails')],
        ])
        await update.message.reply_text(f"Bet locked in: `{bet}`\n\nCall it:", reply_markup=keyboard, parse_mode='Markdown')
        return

    context.user_data.pop('pending_game', None)

    if pending_game == 'mines':
        await play_mines(update, context, bet)
    elif pending_game == 'keno':
        await play_keno(update, context, bet)
    elif pending_game == 'crash':
        await play_crash(update, context, bet)
    else:
        await update.message.reply_text("Unknown game. Send /start to try again.")

async def handle_wallet_text(update, context, text, action):
    if not text.isdigit() or int(text) <= 0:
        await update.message.reply_text("❌ Please type a valid whole number, e.g. `100`", parse_mode='Markdown')
        return

    amount = int(text)
    context.user_data.pop('pending_wallet', None)
    user_id = update.effective_user.id

    if action == 'deposit':
        user = await user_update(user_id, make_deposit(amount))
        await update.message.reply_text(f"✅ Deposited `{amount}` (demo). New balance: `{user['coins']}`", parse_mode='Markdown')
    else:
        try:
            user = await user_update(user_id, make_withdraw(amount))
        except InsufficientFunds:
            await update.message.reply_text("❌ Not enough coins!")
            return
        await update.message.reply_text(f"✅ Withdrew `{amount}` (demo). New balance: `{user['coins']}`", parse_mode='Markdown')

# ===================== NATIVE TELEGRAM GAME PLAY =====================

async def play_native_game(update: Update, context: ContextTypes.DEFAULT_TYPE, game_key: str, bet: int):
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    game = NATIVE_GAMES[game_key]

    try:
        await user_update(user_id, make_deduct(bet))
    except InsufficientFunds:
        await update.message.reply_text("❌ Not enough coins!")
        return

    try:
        dice_msg = await context.bot.send_dice(chat_id=chat_id, emoji=game['emoji'])
        value = dice_msg.dice.value
        await asyncio.sleep(NATIVE_ANIMATION_WAIT)
        mult = game['eval'](value)

        out = {}
        await user_update(user_id, make_apply_result(bet, mult, game_key, out))

        roll_line = f"{game['emoji']} {game['name']} rolled: *{value}*"
        text = summary_text(bet, mult, out['payout'], out['rakeback'], roll_line)
        await context.bot.send_message(
            chat_id=chat_id, text=text,
            reply_markup=result_keyboard(f'bet_{game_key}'), parse_mode='Markdown'
        )
    except Exception as e:
        logger.error(f"Native game error ({game_key}): {e}", exc_info=True)
        await user_update(user_id, make_refund(bet))
        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text="⚠️ Something went wrong and your bet was refunded. Please try again."
            )
        except Exception:
            pass

# ===================== CUSTOM GAME PLAY =====================

async def play_mines(update: Update, context: ContextTypes.DEFAULT_TYPE, bet: int):
    user_id = update.effective_user.id
    try:
        await user_update(user_id, make_deduct(bet))
    except InsufficientFunds:
        await update.message.reply_text("❌ Not enough coins!")
        return
    mult = GameEngine.mines()
    out = {}
    await user_update(user_id, make_apply_result(bet, mult, 'mines', out))
    roll_line = "💣 " + ("Survived the field!" if mult > 0 else "Hit a bomb!")
    text = summary_text(bet, mult, out['payout'], out['rakeback'], roll_line)
    await update.message.reply_text(text, reply_markup=result_keyboard('bet_mines'), parse_mode='Markdown')

async def play_keno(update: Update, context: ContextTypes.DEFAULT_TYPE, bet: int):
    user_id = update.effective_user.id
    try:
        await user_update(user_id, make_deduct(bet))
    except InsufficientFunds:
        await update.message.reply_text("❌ Not enough coins!")
        return
    mult, matches = GameEngine.keno()
    out = {}
    await user_update(user_id, make_apply_result(bet, mult, 'keno', out))
    roll_line = f"🎱 Matched {matches}/5 numbers"
    text = summary_text(bet, mult, out['payout'], out['rakeback'], roll_line)
    await update.message.reply_text(text, reply_markup=result_keyboard('bet_keno'), parse_mode='Markdown')

async def play_crash(update: Update, context: ContextTypes.DEFAULT_TYPE, bet: int):
    user_id = update.effective_user.id
    try:
        await user_update(user_id, make_deduct(bet))
    except InsufficientFunds:
        await update.message.reply_text("❌ Not enough coins!")
        return
    mult, crash_point = GameEngine.crash(2.0)
    out = {}
    await user_update(user_id, make_apply_result(bet, mult, 'crash', out))
    roll_line = f"📈 Crashed at *{crash_point}x* (auto cash-out target: 2x)"
    text = summary_text(bet, mult, out['payout'], out['rakeback'], roll_line)
    await update.message.reply_text(text, reply_markup=result_keyboard('bet_crash'), parse_mode='Markdown')

async def play_coinflip(update: Update, context: ContextTypes.DEFAULT_TYPE, bet: int, choice: str):
    query = update.callback_query
    user_id = query.from_user.id
    try:
        await user_update(user_id, make_deduct(bet))
    except InsufficientFunds:
        await query.answer("Not enough coins!", show_alert=True)
        return
    mult, result = GameEngine.coinflip(choice)
    out = {}
    await user_update(user_id, make_apply_result(bet, mult, 'coinflip', out))
    roll_line = f"🪙 Landed on: *{result}* (you called {choice})"
    text = summary_text(bet, mult, out['payout'], out['rakeback'], roll_line)
    await safe_edit(query, text, reply_markup=result_keyboard('bet_coinflip'))

# ===================== WALLET =====================

async def wallet(update, context):
    query = update.callback_query
    await query.answer()
    user = await get_user(query.from_user.id)
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("💸 Deposit", callback_data='deposit')],
        [InlineKeyboardButton("🏦 Withdraw", callback_data='withdraw')],
        [InlineKeyboardButton("🔙 Back", callback_data='back_to_main')],
    ])
    await safe_edit(
        query,
        f"💰 *Wallet*\n\n"
        f"Balance: `{user['coins']}`\n"
        f"Total Deposited: `{user['wallet']['total_deposited']}`\n"
        f"Total Withdrawn: `{user['wallet']['total_withdrawn']}`\n\n"
        f"⚠️ Demo mode — no real payment processor connected yet.",
        reply_markup=keyboard
    )

async def deposit(update, context):
    query = update.callback_query
    await query.answer()
    context.user_data['pending_wallet'] = 'deposit'
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data='wallet')]])
    await safe_edit(query, "💸 Type the amount to deposit (demo):", reply_markup=keyboard)

async def withdraw(update, context):
    query = update.callback_query
    await query.answer()
    context.user_data['pending_wallet'] = 'withdraw'
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data='wallet')]])
    await safe_edit(query, "🏦 Type the amount to withdraw (demo):", reply_markup=keyboard)

# ===================== STATS / LEADERBOARD / REFERRAL / PROFILE =====================

async def stats(update, context):
    query = update.callback_query
    await query.answer()
    user = await get_user(query.from_user.id)
    vip = get_vip_level(user['xp'])
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data='back_to_main')]])
    await safe_edit(
        query,
        f"📊 *Your Stats*\n\n"
        f"Balance: `{user['coins']}`\n"
        f"VIP: {VIP_NAMES[vip]}\n"
        f"XP: `{user['xp']}`\n\n"
        f"Games Played: `{user['stats']['games_played']}`\n"
        f"Total Wagered: `{user['stats']['total_wagered']}`\n"
        f"Total Won: `{user['stats']['total_won']}`\n"
        f"Biggest Win: `{user['stats']['biggest_win']}`\n\n"
        f"Rakeback: {VIP_RAKEBACK[vip]*100:.0f}%",
        reply_markup=keyboard
    )

async def leaderboard(update, context):
    query = update.callback_query
    await query.answer()
    users = await users_snapshot()
    sorted_users = sorted(users.items(), key=lambda x: x[1]['coins'], reverse=True)[:10]
    text = "🏆 *Top 10 Players*\n\n"
    for i, (uid, u) in enumerate(sorted_users, 1):
        vip = get_vip_level(u['xp'])
        text += f"{i}. {u.get('username', 'Player')} - `{u['coins']}` [{VIP_NAMES[vip]}]\n"
    if not sorted_users:
        text += "No players yet!"
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data='back_to_main')]])
    await safe_edit(query, text, reply_markup=keyboard)

async def referral(update, context):
    query = update.callback_query
    await query.answer()
    user = await get_user(query.from_user.id)
    bot_username = context.bot.username
    link = f"https://t.me/{bot_username}?start={user['ref_code']}"
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data='back_to_main')]])
    await safe_edit(
        query,
        f"🎁 *Referral System*\n\n"
        f"Your Code: `{user['ref_code']}`\n"
        f"Your Link: {link}\n\n"
        f"Referrals: `{len(user['referrals'])}`\n\n"
        f"Share your link — you and your friend both get +150 coins.",
        reply_markup=keyboard
    )

async def profile(update, context):
    query = update.callback_query
    await query.answer()
    user = await get_user(query.from_user.id)
    vip = get_vip_level(user['xp'])
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data='back_to_main')]])
    await safe_edit(
        query,
        f"👤 *{user.get('username', 'Player')}*\n\n"
        f"UID: `{query.from_user.id}`\n"
        f"Balance: `{user['coins']}`\n"
        f"VIP: {VIP_NAMES[vip]}\n"
        f"XP: `{user['xp']}`\n"
        f"Ref Code: `{user['ref_code']}`\n"
        f"Joined: `{user['created_at'][:10]}`",
        reply_markup=keyboard
    )

# ===================== ADMIN =====================

async def admin_panel(update, context):
    query = update.callback_query
    if query.from_user.id not in ADMIN_IDS:
        await query.answer("❌ Unauthorized", show_alert=True)
        return
    await query.answer()
    users = await users_snapshot()
    total_coins = sum(u['coins'] for u in users.values())
    total_users = len(users)
    total_games = sum(u['stats']['games_played'] for u in users.values())
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data='back_to_main')]])
    await safe_edit(
        query,
        f"🔧 *Admin Panel*\n\n"
        f"Total Users: `{total_users}`\n"
        f"Total Coins in economy: `{total_coins}`\n"
        f"Total Games Played: `{total_games}`\n\n"
        f"Storage: `{'Postgres' if isinstance(storage, PostgresStorage) else 'Local JSON'}`",
        reply_markup=keyboard
    )

# ===================== CALLBACK ROUTER =====================

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    data = query.data
    try:
        if data == 'games_menu': await games_menu(update, context)
        elif data == 'back_to_main': await back_to_main(update, context)
        elif data == 'daily_bonus': await daily_bonus(update, context)

        elif data.startswith('bet_'):
            await prompt_bet(update, context, data[len('bet_'):])

        elif data in ('flip_heads', 'flip_tails'):
            bet = context.user_data.pop('coinflip_bet', None)
            if bet is None:
                await query.answer("Bet expired, please start again.", show_alert=True)
                return
            choice = 'heads' if data == 'flip_heads' else 'tails'
            await play_coinflip(update, context, bet, choice)

        elif data == 'wallet': await wallet(update, context)
        elif data == 'deposit': await deposit(update, context)
        elif data == 'withdraw': await withdraw(update, context)

        elif data == 'stats': await stats(update, context)
        elif data == 'leaderboard': await leaderboard(update, context)
        elif data == 'referral': await referral(update, context)
        elif data == 'profile': await profile(update, context)
        elif data == 'admin_panel': await admin_panel(update, context)
        else:
            await query.answer()
    except Exception as e:
        logger.error(f"Error in button handler ({data}): {e}", exc_info=True)
        try:
            await query.answer(f"Error: {str(e)[:180]}", show_alert=True)
        except Exception:
            pass

# ===================== INITIALIZATION =====================

async def init_bot() -> Application:
    global bot_app
    logger.info("Initializing application...")
    application = Application.builder().token(TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_cmd))
    application.add_handler(CommandHandler("daily", daily_bonus))
    application.add_handler(CommandHandler("cancel", cancel))
    application.add_handler(CallbackQueryHandler(button_handler))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    await application.initialize()
    bot_app = application
    logger.info("Bot initialized and ready")
    return bot_app

async def setup_webhook_async():
    try:
        webhook_url = f"{WEBHOOK_URL}/webhook"
        logger.info("Clearing old webhook + pending updates...")
        await bot_app.bot.delete_webhook(drop_pending_updates=True)
        logger.info(f"Setting webhook to {webhook_url}")
        result = await bot_app.bot.set_webhook(url=webhook_url)
        logger.info(f"Webhook set: {result}")
        info = await bot_app.bot.get_webhook_info()
        logger.info(f"Webhook confirmed: url={info.url}, pending={info.pending_update_count}")
    except Exception as e:
        logger.error(f"Webhook setup error: {e}", exc_info=True)

# ===================== FLASK ROUTES =====================

@flask_app.route('/', methods=['GET', 'HEAD'])
def index():
    return 'TokenArcade Bot Running', 200

@flask_app.route('/webhook', methods=['POST'])
def webhook():
    global bot_app, bot_loop
    try:
        if bot_app is None or bot_loop is None:
            logger.error("Bot not ready yet")
            return 'Bot not ready', 503

        data = request.get_json(force=True, silent=True)
        if not data:
            return '', 204

        update = Update.de_json(data, bot_app.bot)
        if not update:
            return '', 204

        if already_processed(update.update_id):
            logger.info(f"Duplicate update {update.update_id} ignored")
            return '', 200

        logger.info(f"Scheduling update {update.update_id}")

        def _on_done(fut):
            exc = fut.exception()
            if exc:
                logger.error(f"Unhandled error processing update {update.update_id}: {exc}", exc_info=exc)

        future = asyncio.run_coroutine_threadsafe(bot_app.process_update(update), bot_loop)
        future.add_done_callback(_on_done)

        return '', 200

    except Exception as e:
        logger.error(f"WEBHOOK ERROR: {e}", exc_info=True)
        return '', 200

# ===================== MAIN =====================

def _run_background_loop(loop: asyncio.AbstractEventLoop):
    asyncio.set_event_loop(loop)
    loop.run_forever()

def _run_flask():
    if WAITRESS_AVAILABLE:
        logger.info("Serving with waitress (production WSGI server)")
        waitress_serve(flask_app, host='0.0.0.0', port=PORT, threads=8)
    else:
        logger.warning("waitress not installed - falling back to Flask dev server")
        flask_app.run(host='0.0.0.0', port=PORT, debug=False, use_reloader=False, threaded=True)

if __name__ == '__main__':
    bot_loop = asyncio.new_event_loop()
    threading.Thread(target=_run_background_loop, args=(bot_loop,), daemon=True).start()

    logger.info("Starting TokenArcade Bot")

    init_future = asyncio.run_coroutine_threadsafe(init_bot(), bot_loop)
    init_future.result(timeout=30)

    flask_thread = threading.Thread(target=_run_flask, daemon=True)
    flask_thread.start()

    logger.info("Waiting for the web server to be ready...")
    for attempt in range(30):
        try:
            r = requests.get(f"http://localhost:{PORT}/", timeout=2)
            if r.status_code == 200:
                logger.info(f"Server ready (attempt {attempt + 1})")
                break
        except Exception:
            pass
        time.sleep(1)
    else:
        logger.error("Server didn't start in time!")

    webhook_future = asyncio.run_coroutine_threadsafe(setup_webhook_async(), bot_loop)
    webhook_future.result(timeout=30)

    logger.info("Bot fully running.")
    flask_thread.join()
