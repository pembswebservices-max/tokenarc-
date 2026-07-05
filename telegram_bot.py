import os
import json
import logging
import asyncio
import time
import threading
import random
import requests
from datetime import datetime
from typing import Optional, Dict
from enum import Enum

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)
from flask import Flask, request

# ===================== LOGGING =====================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)
logging.getLogger('httpx').setLevel(logging.WARNING)
logging.getLogger('httpcore').setLevel(logging.WARNING)
logging.getLogger('urllib3').setLevel(logging.WARNING)

# ===================== CONFIG =====================
TOKEN = os.getenv('TELEGRAM_TOKEN')
WEBHOOK_URL = os.getenv('WEBHOOK_URL', '').strip()
PORT = int(os.getenv('PORT', 8000))
ADMIN_IDS = [int(x) for x in os.getenv('ADMIN_IDS', '').split(',') if x.strip()]
DATA_FILE = 'arcade_users.json'

if not TOKEN or not WEBHOOK_URL:
    raise ValueError("❌ Missing TELEGRAM_TOKEN or WEBHOOK_URL")

logger.info(f"✅ Bot configured - Webhook: {WEBHOOK_URL}")

# ===================== FLASK / GLOBALS =====================
flask_app = Flask(__name__)
bot_app: Optional[Application] = None
loop: Optional[asyncio.AbstractEventLoop] = None
loop_lock = threading.Lock()

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
BET_OPTIONS = [10, 50, 100, 500]

# ===================== STORAGE =====================
storage_lock = threading.Lock()

class UserManager:
    @staticmethod
    def load_users() -> Dict:
        with storage_lock:
            if os.path.exists(DATA_FILE):
                try:
                    with open(DATA_FILE, 'r') as f:
                        return json.load(f)
                except Exception as e:
                    logger.error(f"Error loading users: {e}")
            return {}

    @staticmethod
    def save_users(users: Dict) -> None:
        with storage_lock:
            try:
                with open(DATA_FILE, 'w') as f:
                    json.dump(users, f, indent=2)
            except Exception as e:
                logger.error(f"Error saving users: {e}")

    @staticmethod
    def get_or_create(user_id: int, users: Dict) -> Dict:
        uid = str(user_id)
        if uid not in users:
            users[uid] = {
                'username': '',
                'coins': 1000,
                'xp': 0,
                'ref_code': f"TG{user_id % 100000:05d}",
                'referrer': None,
                'referrals': [],
                'affiliate_earnings': 0,
                'wallet': {'total_deposited': 0, 'total_withdrawn': 0, 'pending_withdrawal': 0},
                'stats': {
                    'games_played': 0, 'total_wagered': 0, 'total_won': 0,
                    'biggest_win': 0, 'last_daily': None, 'game_history': {}
                },
                'created_at': datetime.now().isoformat(),
            }
        return users[uid]

    @staticmethod
    def get_vip_level(xp: int) -> VIPLevel:
        if xp >= 50000: return VIPLevel.DIAMOND
        if xp >= 15000: return VIPLevel.PLATINUM
        if xp >= 5000: return VIPLevel.GOLD
        if xp >= 1000: return VIPLevel.SILVER
        return VIPLevel.BRONZE

# ===================== GAME ENGINE =====================
class GameEngine:
    @staticmethod
    def mines(bet: int, bombs: int = 5):
        safe_tiles = random.randint(1, min(8, 25 - bombs))
        mult = 1.0
        for i in range(safe_tiles):
            mult *= (25 - i) / (25 - i - bombs)
        mult *= 0.95
        hit_bomb = random.random() < (safe_tiles / (25 - bombs))
        if hit_bomb:
            return False, 0
        return True, int(bet * mult)

    @staticmethod
    def keno(bet: int):
        drawn = set(random.sample(range(1, 41), 10))
        picks = set(random.sample(range(1, 41), 5))
        matches = len(picks & drawn)
        payout_table = {0: 0, 1: 0, 2: 0.5, 3: 1.5, 4: 4, 5: 15}
        mult = payout_table.get(matches, 0)
        return int(bet * mult), matches

    @staticmethod
    def slots(bet: int):
        symbols = ['🍒', '🍋', '🍊', '🔔', '💎', '7️⃣']
        reels = [random.choice(symbols) for _ in range(3)]
        mult = 0
        if reels[0] == reels[1] == reels[2]:
            mult = 100 if reels[0] == '7️⃣' else (50 if reels[0] == '💎' else 10)
        elif reels[0] == reels[1] or reels[1] == reels[2] or reels[0] == reels[2]:
            mult = 2
        return int(bet * mult), ''.join(reels)

    @staticmethod
    def coinflip(bet: int, choice: str):
        result = random.choice(['heads', 'tails'])
        won = (result == choice)
        return won, (bet * 2 if won else 0), result

    @staticmethod
    def crash(bet: int, cashout_target: float):
        crash_point = round(random.uniform(1.0, 8.0), 2)
        won = cashout_target <= crash_point
        payout = int(bet * cashout_target) if won else 0
        return won, payout, crash_point

    @staticmethod
    def dice(bet: int, prediction: str):
        d1, d2 = random.randint(1, 6), random.randint(1, 6)
        total = d1 + d2
        is_high = total >= 7
        won = (is_high and prediction == 'high') or (not is_high and prediction == 'low')
        return won, (bet * 2 if won else 0), d1, d2, total

# ===================== HELPERS =====================

def apply_result(user: dict, bet: int, payout: int, game_key: str):
    """Apply bet/payout/xp/rakeback to a user dict. Returns extra text (rakeback line)."""
    user['coins'] += payout
    if payout > 0:
        user['stats']['total_won'] += payout
        if payout > user['stats']['biggest_win']:
            user['stats']['biggest_win'] = payout
    user['stats']['games_played'] += 1
    user['stats']['total_wagered'] += bet
    user['xp'] += max(1, bet // 10)
    user['stats']['game_history'][game_key] = user['stats']['game_history'].get(game_key, 0) + 1

    vip = UserManager.get_vip_level(user['xp'])
    rakeback = int(bet * VIP_RAKEBACK[vip])
    if rakeback > 0:
        user['coins'] += rakeback
        return f"\n🎁 VIP Rakeback: +{rakeback}"
    return ""

def main_menu_keyboard(user_id: int):
    keyboard = [
        [InlineKeyboardButton("🎮 Play Games", callback_data='games_menu')],
        [InlineKeyboardButton("💰 Wallet", callback_data='wallet'), InlineKeyboardButton("📊 Stats", callback_data='stats')],
        [InlineKeyboardButton("🏆 Leaderboard", callback_data='leaderboard'), InlineKeyboardButton("🎁 Referral", callback_data='referral')],
        [InlineKeyboardButton("👤 Profile", callback_data='profile')],
    ]
    if user_id in ADMIN_IDS:
        keyboard.append([InlineKeyboardButton("🔧 Admin Panel", callback_data='admin_panel')])
    return InlineKeyboardMarkup(keyboard)

def main_menu_text(user: dict) -> str:
    vip = UserManager.get_vip_level(user['xp'])
    return (
        f"🎰 *TokenArcade*\n\n"
        f"💰 Balance: `{user['coins']}¢`\n"
        f"⭐ VIP: {VIP_NAMES[vip]}\n\n"
        f"Choose an option:"
    )

def bet_keyboard(game_prefix: str, back_target: str = 'games_menu', extra_rows=None):
    rows = [[InlineKeyboardButton(f"Bet {b}", callback_data=f'{game_prefix}_{b}') for b in BET_OPTIONS[:2]],
            [InlineKeyboardButton(f"Bet {b}", callback_data=f'{game_prefix}_{b}') for b in BET_OPTIONS[2:]]]
    if extra_rows:
        rows = extra_rows + rows
    rows.append([InlineKeyboardButton("🔙 Back", callback_data=back_target)])
    return InlineKeyboardMarkup(rows)

def result_keyboard(replay_target: str):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Play Again", callback_data=replay_target)],
        [InlineKeyboardButton("🎮 Other Games", callback_data='games_menu')],
        [InlineKeyboardButton("🏠 Main Menu", callback_data='back_to_main')],
    ])

# ===================== HANDLERS: CORE =====================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.info(f"🎯 /start from {update.effective_user.first_name} ({update.effective_user.id})")
    users = UserManager.load_users()
    user = UserManager.get_or_create(update.effective_user.id, users)
    user['username'] = update.effective_user.first_name or 'Player'

    # Referral capture: /start REFCODE
    if context.args and not user.get('referrer'):
        ref_code = context.args[0].strip()
        for uid, u in users.items():
            if u.get('ref_code') == ref_code and uid != str(update.effective_user.id):
                user['referrer'] = uid
                u.setdefault('referrals', [])
                if str(update.effective_user.id) not in u['referrals']:
                    u['referrals'].append(str(update.effective_user.id))
                    user['coins'] += 150
                    u['coins'] += 150
                break

    UserManager.save_users(users)

    await update.message.reply_text(
        f"🎰 *Welcome to TokenArcade!*\n\n"
        f"Hi {user['username']}!\n\n"
        f"💰 Balance: `{user['coins']}¢`\n"
        f"🎁 Your Referral Code: `{user['ref_code']}`\n\n"
        f"Choose an option below:",
        reply_markup=main_menu_keyboard(update.effective_user.id),
        parse_mode='Markdown'
    )
    logger.info(f"✅ Reply sent to {user['username']}")

async def games_menu(update, context):
    query = update.callback_query
    await query.answer()
    keyboard = [
        [InlineKeyboardButton("💣 Mines", callback_data='game_mines')],
        [InlineKeyboardButton("🎱 Keno", callback_data='game_keno')],
        [InlineKeyboardButton("🎰 Slots", callback_data='game_slots')],
        [InlineKeyboardButton("🎲 Dice", callback_data='game_dice')],
        [InlineKeyboardButton("🪙 Coinflip", callback_data='game_coinflip')],
        [InlineKeyboardButton("📈 Crash", callback_data='game_crash')],
        [InlineKeyboardButton("🔙 Back", callback_data='back_to_main')],
    ]
    await query.edit_message_text(
        "🎮 *Choose a Game*\n\n"
        "💣 Mines - Dodge bombs (up to ~25x)\n"
        "🎱 Keno - Match numbers (up to 15x)\n"
        "🎰 Slots - Spin to win (up to 100x)\n"
        "🎲 Dice - Predict high/low (2x)\n"
        "🪙 Coinflip - 50/50 double (2x)\n"
        "📈 Crash - Cash out before it crashes (up to 3x auto)\n",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='Markdown'
    )

async def back_to_main(update, context):
    query = update.callback_query
    users = UserManager.load_users()
    user = UserManager.get_or_create(query.from_user.id, users)
    UserManager.save_users(users)
    await query.edit_message_text(
        main_menu_text(user),
        reply_markup=main_menu_keyboard(query.from_user.id),
        parse_mode='Markdown'
    )

# ===================== MINES =====================

async def game_mines(update, context):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "💣 *Mines*\n\nDodge the bombs and escalate your multiplier!\n\nSelect your bet:",
        reply_markup=bet_keyboard('mines'),
        parse_mode='Markdown'
    )

async def mines_play(update, context, bet):
    query = update.callback_query
    users = UserManager.load_users()
    user = UserManager.get_or_create(query.from_user.id, users)

    if bet > user['coins']:
        await query.answer(f"❌ Not enough coins! You have {user['coins']}", show_alert=True)
        return

    user['coins'] -= bet
    won, payout = GameEngine.mines(bet, 5)
    text = f"✅ *Won {payout}!*" if won else f"💥 *Boom! Lost {bet}*"
    extra = apply_result(user, bet, payout, 'mines')
    UserManager.save_users(users)

    await query.edit_message_text(
        f"{text}\n\nNew balance: `{user['coins']}`{extra}",
        reply_markup=result_keyboard('game_mines'),
        parse_mode='Markdown'
    )

# ===================== KENO =====================

async def game_keno(update, context):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "🎱 *Keno*\n\n5 random numbers picked for you vs 10 drawn. Match more, win more!\n\nSelect your bet:",
        reply_markup=bet_keyboard('keno'),
        parse_mode='Markdown'
    )

async def keno_play(update, context, bet):
    query = update.callback_query
    users = UserManager.load_users()
    user = UserManager.get_or_create(query.from_user.id, users)

    if bet > user['coins']:
        await query.answer(f"❌ Not enough coins! You have {user['coins']}", show_alert=True)
        return

    user['coins'] -= bet
    payout, matches = GameEngine.keno(bet)
    text = f"🎱 Matched {matches}/5\n" + (f"✅ *Won {payout}!*" if payout > 0 else f"❌ *Lost {bet}*")
    extra = apply_result(user, bet, payout, 'keno')
    UserManager.save_users(users)

    await query.edit_message_text(
        f"{text}\n\nNew balance: `{user['coins']}`{extra}",
        reply_markup=result_keyboard('game_keno'),
        parse_mode='Markdown'
    )

# ===================== SLOTS =====================

async def game_slots(update, context):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "🎰 *Slots*\n\nSpin 3 reels. Match all 3 for up to 100x!\n\nSelect your bet:",
        reply_markup=bet_keyboard('slots'),
        parse_mode='Markdown'
    )

async def slots_play(update, context, bet):
    query = update.callback_query
    users = UserManager.load_users()
    user = UserManager.get_or_create(query.from_user.id, users)

    if bet > user['coins']:
        await query.answer(f"❌ Not enough coins! You have {user['coins']}", show_alert=True)
        return

    user['coins'] -= bet
    payout, reels = GameEngine.slots(bet)
    text = f"🎰 {reels}\n" + (f"✅ *Won {payout}!*" if payout > 0 else "❌ *No match*")
    extra = apply_result(user, bet, payout, 'slots')
    UserManager.save_users(users)

    await query.edit_message_text(
        f"{text}\n\nNew balance: `{user['coins']}`{extra}",
        reply_markup=result_keyboard('game_slots'),
        parse_mode='Markdown'
    )

# ===================== DICE =====================

async def game_dice(update, context):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "🎲 *Dice*\n\nPredict if the total of 2 dice will be High (7-12) or Low (2-6). 2x payout.\n\nSelect your bet:",
        reply_markup=bet_keyboard('dice'),
        parse_mode='Markdown'
    )

async def dice_pick_side(update, context, bet):
    query = update.callback_query
    await query.answer()
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("⬆️ High (7-12)", callback_data=f'diceroll_{bet}_high')],
        [InlineKeyboardButton("⬇️ Low (2-6)", callback_data=f'diceroll_{bet}_low')],
        [InlineKeyboardButton("🔙 Back", callback_data='game_dice')],
    ])
    await query.edit_message_text(f"🎲 Bet: `{bet}`\n\nPick High or Low:", reply_markup=keyboard, parse_mode='Markdown')

async def dice_play(update, context, bet, prediction):
    query = update.callback_query
    users = UserManager.load_users()
    user = UserManager.get_or_create(query.from_user.id, users)

    if bet > user['coins']:
        await query.answer(f"❌ Not enough coins! You have {user['coins']}", show_alert=True)
        return

    user['coins'] -= bet
    won, payout, d1, d2, total = GameEngine.dice(bet, prediction)
    text = f"🎲 {d1} + {d2} = {total}\n" + (f"✅ *Won {payout}!*" if won else f"❌ *Lost {bet}*")
    extra = apply_result(user, bet, payout, 'dice')
    UserManager.save_users(users)

    await query.edit_message_text(
        f"{text}\n\nNew balance: `{user['coins']}`{extra}",
        reply_markup=result_keyboard('game_dice'),
        parse_mode='Markdown'
    )

# ===================== COINFLIP =====================

async def game_coinflip(update, context):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "🪙 *Coinflip*\n\n50/50 double or nothing.\n\nSelect your bet:",
        reply_markup=bet_keyboard('coinflip'),
        parse_mode='Markdown'
    )

async def coinflip_pick_side(update, context, bet):
    query = update.callback_query
    await query.answer()
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🪙 Heads", callback_data=f'flip_{bet}_heads')],
        [InlineKeyboardButton("🪙 Tails", callback_data=f'flip_{bet}_tails')],
        [InlineKeyboardButton("🔙 Back", callback_data='game_coinflip')],
    ])
    await query.edit_message_text(f"🪙 Bet: `{bet}`\n\nPick a side:", reply_markup=keyboard, parse_mode='Markdown')

async def coinflip_play(update, context, bet, choice):
    query = update.callback_query
    users = UserManager.load_users()
    user = UserManager.get_or_create(query.from_user.id, users)

    if bet > user['coins']:
        await query.answer(f"❌ Not enough coins! You have {user['coins']}", show_alert=True)
        return

    user['coins'] -= bet
    won, payout, result = GameEngine.coinflip(bet, choice)
    text = f"🪙 Landed on: *{result}*\n" + (f"✅ *Won {payout}!*" if won else f"❌ *Lost {bet}*")
    extra = apply_result(user, bet, payout, 'coinflip')
    UserManager.save_users(users)

    await query.edit_message_text(
        f"{text}\n\nNew balance: `{user['coins']}`{extra}",
        reply_markup=result_keyboard('game_coinflip'),
        parse_mode='Markdown'
    )

# ===================== CRASH =====================

async def game_crash(update, context):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "📈 *Crash*\n\nAuto cash-out at 2x. If the crash point is above 2x, you win!\n\nSelect your bet:",
        reply_markup=bet_keyboard('crash'),
        parse_mode='Markdown'
    )

async def crash_play(update, context, bet):
    query = update.callback_query
    users = UserManager.load_users()
    user = UserManager.get_or_create(query.from_user.id, users)

    if bet > user['coins']:
        await query.answer(f"❌ Not enough coins! You have {user['coins']}", show_alert=True)
        return

    user['coins'] -= bet
    cashout_target = 2.0
    won, payout, crash_point = GameEngine.crash(bet, cashout_target)
    text = f"📈 Crashed at {crash_point}x (auto cash-out: {cashout_target}x)\n" + (f"✅ *Won {payout}!*" if won else f"❌ *Lost {bet}*")
    extra = apply_result(user, bet, payout, 'crash')
    UserManager.save_users(users)

    await query.edit_message_text(
        f"{text}\n\nNew balance: `{user['coins']}`{extra}",
        reply_markup=result_keyboard('game_crash'),
        parse_mode='Markdown'
    )

# ===================== WALLET =====================

async def wallet(update, context):
    query = update.callback_query
    await query.answer()
    users = UserManager.load_users()
    user = UserManager.get_or_create(query.from_user.id, users)
    UserManager.save_users(users)

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("💸 Deposit", callback_data='deposit')],
        [InlineKeyboardButton("🏦 Withdraw", callback_data='withdraw')],
        [InlineKeyboardButton("🔙 Back", callback_data='back_to_main')],
    ])
    await query.edit_message_text(
        f"💰 *Wallet*\n\n"
        f"Balance: `{user['coins']}¢`\n"
        f"Total Deposited: `{user['wallet']['total_deposited']}`\n"
        f"Total Withdrawn: `{user['wallet']['total_withdrawn']}`\n\n"
        f"⚠️ Demo mode: no real payment processor connected yet.",
        reply_markup=keyboard,
        parse_mode='Markdown'
    )

async def deposit(update, context):
    query = update.callback_query
    await query.answer()
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"+{amt}", callback_data=f'dodeposit_{amt}') for amt in [100, 500, 1000]],
        [InlineKeyboardButton("🔙 Back", callback_data='wallet')],
    ])
    await query.edit_message_text(
        "💸 *Deposit (Demo)*\n\nNo real money involved — this simulates adding coins.\nPick an amount:",
        reply_markup=keyboard, parse_mode='Markdown'
    )

async def do_deposit(update, context, amount):
    query = update.callback_query
    users = UserManager.load_users()
    user = UserManager.get_or_create(query.from_user.id, users)
    user['coins'] += amount
    user['wallet']['total_deposited'] += amount
    UserManager.save_users(users)
    await query.answer(f"✅ +{amount} coins added!", show_alert=True)
    await wallet(update, context)

async def withdraw(update, context):
    query = update.callback_query
    await query.answer()
    users = UserManager.load_users()
    user = UserManager.get_or_create(query.from_user.id, users)
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"-{amt}", callback_data=f'dowithdraw_{amt}') for amt in [100, 500, 1000] if amt <= user['coins']],
        [InlineKeyboardButton("🔙 Back", callback_data='wallet')],
    ])
    await query.edit_message_text(
        f"🏦 *Withdraw (Demo)*\n\nBalance: `{user['coins']}¢`\nPick an amount:",
        reply_markup=keyboard, parse_mode='Markdown'
    )

async def do_withdraw(update, context, amount):
    query = update.callback_query
    users = UserManager.load_users()
    user = UserManager.get_or_create(query.from_user.id, users)
    if amount > user['coins']:
        await query.answer("❌ Not enough coins!", show_alert=True)
        return
    user['coins'] -= amount
    user['wallet']['total_withdrawn'] += amount
    UserManager.save_users(users)
    await query.answer(f"✅ Withdrew {amount} coins (demo)", show_alert=True)
    await wallet(update, context)

# ===================== STATS / LEADERBOARD / REFERRAL / PROFILE =====================

async def stats(update, context):
    query = update.callback_query
    await query.answer()
    users = UserManager.load_users()
    user = UserManager.get_or_create(query.from_user.id, users)
    UserManager.save_users(users)
    vip = UserManager.get_vip_level(user['xp'])
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data='back_to_main')]])
    await query.edit_message_text(
        f"📊 *Your Stats*\n\n"
        f"💰 Balance: `{user['coins']}¢`\n"
        f"⭐ VIP: {VIP_NAMES[vip]}\n"
        f"⚡ XP: `{user['xp']}`\n\n"
        f"🎮 Games Played: `{user['stats']['games_played']}`\n"
        f"💸 Total Wagered: `{user['stats']['total_wagered']}`\n"
        f"🏆 Total Won: `{user['stats']['total_won']}`\n"
        f"💎 Biggest Win: `{user['stats']['biggest_win']}`\n\n"
        f"🎁 Rakeback: {VIP_RAKEBACK[vip]*100:.0f}%",
        reply_markup=keyboard, parse_mode='Markdown'
    )

async def leaderboard(update, context):
    query = update.callback_query
    await query.answer()
    users = UserManager.load_users()
    sorted_users = sorted(users.items(), key=lambda x: x[1]['coins'], reverse=True)[:10]
    text = "🏆 *Top 10 Players*\n\n"
    for i, (uid, u) in enumerate(sorted_users, 1):
        vip = UserManager.get_vip_level(u['xp'])
        text += f"{i}. {u.get('username', 'Player')} - `{u['coins']}¢` [{VIP_NAMES[vip]}]\n"
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data='back_to_main')]])
    await query.edit_message_text(text, reply_markup=keyboard, parse_mode='Markdown')

async def referral(update, context):
    query = update.callback_query
    await query.answer()
    users = UserManager.load_users()
    user = UserManager.get_or_create(query.from_user.id, users)
    UserManager.save_users(users)
    bot_username = context.bot.username
    link = f"https://t.me/{bot_username}?start={user['ref_code']}"
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data='back_to_main')]])
    await query.edit_message_text(
        f"🎁 *Referral System*\n\n"
        f"Your Code: `{user['ref_code']}`\n"
        f"Your Link: {link}\n\n"
        f"📊 Referrals: `{len(user['referrals'])}`\n\n"
        f"Share your link — you and your friend both get +150 coins when they join!",
        reply_markup=keyboard, parse_mode='Markdown', disable_web_page_preview=True
    )

async def profile(update, context):
    query = update.callback_query
    await query.answer()
    users = UserManager.load_users()
    user = UserManager.get_or_create(query.from_user.id, users)
    UserManager.save_users(users)
    vip = UserManager.get_vip_level(user['xp'])
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data='back_to_main')]])
    await query.edit_message_text(
        f"👤 *{user.get('username', 'Player')}*\n\n"
        f"🆔 UID: `{query.from_user.id}`\n"
        f"💰 Balance: `{user['coins']}¢`\n"
        f"⭐ VIP: {VIP_NAMES[vip]}\n"
        f"⚡ XP: `{user['xp']}`\n"
        f"🎁 Ref Code: `{user['ref_code']}`\n"
        f"📅 Joined: `{user['created_at'][:10]}`",
        reply_markup=keyboard, parse_mode='Markdown'
    )

# ===================== ADMIN =====================

async def admin_panel(update, context):
    query = update.callback_query
    if query.from_user.id not in ADMIN_IDS:
        await query.answer("❌ Unauthorized", show_alert=True)
        return
    await query.answer()
    users = UserManager.load_users()
    total_coins = sum(u['coins'] for u in users.values())
    total_users = len(users)
    total_games = sum(u['stats']['games_played'] for u in users.values())
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data='back_to_main')]])
    await query.edit_message_text(
        f"🔧 *Admin Panel*\n\n"
        f"👥 Total Users: `{total_users}`\n"
        f"💰 Total Coins in economy: `{total_coins}¢`\n"
        f"🎮 Total Games Played: `{total_games}`",
        reply_markup=keyboard, parse_mode='Markdown'
    )

# ===================== CALLBACK ROUTER =====================

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    data = query.data
    try:
        if data == 'games_menu': await games_menu(update, context)
        elif data == 'back_to_main': await back_to_main(update, context)

        elif data == 'game_mines': await game_mines(update, context)
        elif data.startswith('mines_'): await mines_play(update, context, int(data.split('_')[1]))

        elif data == 'game_keno': await game_keno(update, context)
        elif data.startswith('keno_'): await keno_play(update, context, int(data.split('_')[1]))

        elif data == 'game_slots': await game_slots(update, context)
        elif data.startswith('slots_'): await slots_play(update, context, int(data.split('_')[1]))

        elif data == 'game_dice': await game_dice(update, context)
        elif data.startswith('dice_'): await dice_pick_side(update, context, int(data.split('_')[1]))
        elif data.startswith('diceroll_'):
            _, bet, pred = data.split('_')
            await dice_play(update, context, int(bet), pred)

        elif data == 'game_coinflip': await game_coinflip(update, context)
        elif data.startswith('coinflip_'): await coinflip_pick_side(update, context, int(data.split('_')[1]))
        elif data.startswith('flip_'):
            _, bet, side = data.split('_')
            await coinflip_play(update, context, int(bet), side)

        elif data == 'game_crash': await game_crash(update, context)
        elif data.startswith('crash_'): await crash_play(update, context, int(data.split('_')[1]))

        elif data == 'wallet': await wallet(update, context)
        elif data == 'deposit': await deposit(update, context)
        elif data.startswith('dodeposit_'): await do_deposit(update, context, int(data.split('_')[1]))
        elif data == 'withdraw': await withdraw(update, context)
        elif data.startswith('dowithdraw_'): await do_withdraw(update, context, int(data.split('_')[1]))

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
            await query.answer(f"❌ Error: {str(e)[:180]}", show_alert=True)
        except Exception:
            pass

# ===================== INITIALIZATION =====================

async def init_bot() -> Application:
    global bot_app
    logger.info("🔧 Initializing application...")
    bot_app = Application.builder().token(TOKEN).build()
    bot_app.add_handler(CommandHandler("start", start))
    bot_app.add_handler(CallbackQueryHandler(button_handler))
    await bot_app.initialize()
    logger.info("✅ Bot initialized and ready")
    return bot_app

def init_bot_sync():
    global bot_app, loop
    if bot_app is None:
        bot_app = loop.run_until_complete(init_bot())

async def setup_webhook_async():
    global bot_app
    try:
        webhook_url = f"{WEBHOOK_URL}/webhook"
        logger.info("🔗 Clearing old webhook + pending updates...")
        await bot_app.bot.delete_webhook(drop_pending_updates=True)
        logger.info(f"🔗 Setting webhook to {webhook_url}")
        result = await bot_app.bot.set_webhook(url=webhook_url)
        logger.info(f"✅ Webhook set: {result}")
        info = await bot_app.bot.get_webhook_info()
        logger.info(f"✅ Webhook confirmed: url={info.url}, pending={info.pending_update_count}")
    except Exception as e:
        logger.error(f"❌ Webhook setup error: {e}", exc_info=True)

def setup_webhook_sync():
    global loop
    loop.run_until_complete(setup_webhook_async())

# ===================== FLASK ROUTES =====================

@flask_app.route('/', methods=['GET', 'HEAD'])
def index():
    return '✅ TokenArcade Bot Running', 200

@flask_app.route('/webhook', methods=['POST'])
def webhook():
    global bot_app, loop
    try:
        if bot_app is None:
            logger.error("❌ bot_app is None!")
            return 'Bot not ready', 503

        data = request.get_json(force=True, silent=True)
        if not data:
            return '', 204

        update = Update.de_json(data, bot_app.bot)
        if not update:
            return '', 204

        logger.info(f"🔔 Update {update.update_id}")
        with loop_lock:
            loop.run_until_complete(bot_app.process_update(update))
        return '', 200

    except Exception as e:
        logger.error(f"❌ WEBHOOK ERROR: {e}", exc_info=True)
        return '', 200

# ===================== MAIN =====================

if __name__ == '__main__':
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    logger.info("🚀 Starting TokenArcade Bot (Full Version)")
    init_bot_sync()

    def wait_and_setup_webhook():
        logger.info("⏳ Waiting for Flask to be ready...")
        for attempt in range(30):
            try:
                response = requests.get(f"http://localhost:{PORT}/", timeout=2)
                if response.status_code == 200:
                    logger.info(f"✅ Flask ready (attempt {attempt + 1})")
                    time.sleep(1)
                    setup_webhook_sync()
                    return
            except Exception:
                time.sleep(1)
        logger.error("❌ Flask didn't start in time!")

    threading.Thread(target=wait_and_setup_webhook, daemon=True).start()

    logger.info(f"🌍 Flask starting on 0.0.0.0:{PORT}")
    flask_app.run(
        host='0.0.0.0',
        port=PORT,
        debug=False,
        use_reloader=False,
        threaded=False
    )
