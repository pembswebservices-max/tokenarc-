import os
import json
import logging
import asyncio
import random
from datetime import datetime, timedelta
from typing import Optional, Dict
from enum import Enum

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ChatPermissions
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
    ContextTypes,
)
from telegram.error import TelegramError
from flask import Flask, request

# ===================== LOGGING =====================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ===================== CONFIG =====================
TOKEN = os.getenv('TELEGRAM_TOKEN')
WEBHOOK_URL = os.getenv('WEBHOOK_URL')
PORT = int(os.getenv('PORT', 8000))
ADMIN_IDS = [int(x) for x in os.getenv('ADMIN_IDS', '').split(',') if x.strip()]  # Add admin IDs as env var
DATA_FILE = 'arcade_users.json'

if not TOKEN or not WEBHOOK_URL:
    raise ValueError("❌ Missing TELEGRAM_TOKEN or WEBHOOK_URL")

logger.info(f"✅ Bot configured - Webhook: {WEBHOOK_URL}")

# ===================== FLASK =====================
flask_app = Flask(__name__)
bot_app: Optional[Application] = None
loop: Optional[asyncio.AbstractEventLoop] = None

# ===================== ENUMS =====================
class GameType(str, Enum):
    MINES = "mines"
    KENO = "keno"
    CRASH = "crash"
    COINFLIP = "coinflip"
    DICE = "dice"
    SLOTS = "slots"

class VIPLevel(int, Enum):
    BRONZE = 0      # 0 XP
    SILVER = 1      # 1000 XP
    GOLD = 2        # 5000 XP
    PLATINUM = 3    # 15000 XP
    DIAMOND = 4     # 50000 XP

VIP_NAMES = {
    VIPLevel.BRONZE: "Bronze",
    VIPLevel.SILVER: "Silver",
    VIPLevel.GOLD: "Gold",
    VIPLevel.PLATINUM: "Platinum",
    VIPLevel.DIAMOND: "Diamond"
}

VIP_MULTIPLIERS = {
    VIPLevel.BRONZE: 1.0,
    VIPLevel.SILVER: 1.05,
    VIPLevel.GOLD: 1.10,
    VIPLevel.PLATINUM: 1.15,
    VIPLevel.DIAMOND: 1.25
}

VIP_RAKEBACK = {
    VIPLevel.BRONZE: 0,
    VIPLevel.SILVER: 0.01,      # 1%
    VIPLevel.GOLD: 0.02,         # 2%
    VIPLevel.PLATINUM: 0.03,     # 3%
    VIPLevel.DIAMOND: 0.05       # 5%
}

# ===================== STORAGE =====================
class UserManager:
    @staticmethod
    def load_users() -> Dict:
        if os.path.exists(DATA_FILE):
            try:
                with open(DATA_FILE, 'r') as f:
                    return json.load(f)
            except Exception as e:
                logger.error(f"Error loading users: {e}")
        return {}

    @staticmethod
    def save_users(users: Dict) -> None:
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
                'coins': 1000,  # Starting balance
                'xp': 0,
                'level': 1,
                'vip': 0,
                'ref_code': f"TG{user_id % 100000:05d}",
                'referrer': None,
                'referrals': [],
                'affiliate_earnings': 0,
                'wallet': {
                    'total_deposited': 0,
                    'total_withdrawn': 0,
                    'pending_withdrawal': 0,
                },
                'stats': {
                    'games_played': 0,
                    'total_wagered': 0,
                    'total_won': 0,
                    'biggest_win': 0,
                    'last_daily': None,
                    'last_weekly': None,
                    'game_history': {}
                },
                'preferences': {
                    'notifications': True,
                    'language': 'en'
                },
                'created_at': datetime.now().isoformat(),
                'last_active': datetime.now().isoformat()
            }
            UserManager.save_users(users)
        return users[uid]

    @staticmethod
    def get_vip_level(xp: int) -> VIPLevel:
        if xp >= 50000:
            return VIPLevel.DIAMOND
        elif xp >= 15000:
            return VIPLevel.PLATINUM
        elif xp >= 5000:
            return VIPLevel.GOLD
        elif xp >= 1000:
            return VIPLevel.SILVER
        else:
            return VIPLevel.BRONZE

# ===================== GAME ENGINES =====================
class GameEngine:
    @staticmethod
    def mines(bet: int, bombs: int) -> tuple[bool, int]:
        """Mines game: returns (won, payout)"""
        if bombs not in [3, 5, 10]:
            return False, 0
        
        safe_tiles = random.randint(1, min(8, 25 - bombs))
        mult = 1.0
        for i in range(safe_tiles):
            mult *= (25 - i) / (25 - i - bombs)
        mult *= 0.95  # House edge
        
        hit_bomb = random.random() < (safe_tiles / (25 - bombs))
        
        if hit_bomb:
            return False, 0
        else:
            payout = int(bet * mult)
            return True, payout

    @staticmethod
    def keno(bet: int, picks: int = 5) -> tuple[bool, int]:
        """Keno game"""
        drawn = set()
        while len(drawn) < 10:
            drawn.add(random.randint(1, 40))
        
        player_picks = set(random.sample(range(1, 41), min(picks, 10)))
        matches = len(player_picks & drawn)
        
        # Simple payout table
        payout_table = {
            3: [0, 0, 1, 5],
            4: [0, 0, 0, 1.5, 4],
            5: [0, 0, 0, 1, 2, 5],
            6: [0, 0, 0, 0.5, 1, 3, 10]
        }
        
        mult = payout_table.get(picks, {}).get(matches, 0)
        payout = int(bet * mult)
        return payout > 0, payout

    @staticmethod
    def slots(bet: int) -> tuple[bool, int]:
        """Slot machine"""
        symbols = ['🍒', '🍋', '🍊', '🔔', '💎', '7️⃣']
        reels = [random.choice(symbols) for _ in range(3)]
        
        mult = 0
        if reels[0] == reels[1] == reels[2]:
            if reels[0] == '7️⃣':
                mult = 100
            elif reels[0] == '💎':
                mult = 50
            else:
                mult = 10
        elif reels[0] == reels[1] or reels[1] == reels[2] or reels[0] == reels[2]:
            mult = 2
        
        payout = int(bet * mult)
        return payout > 0, payout, ''.join(reels)

    @staticmethod
    def coinflip(bet: int) -> tuple[bool, int]:
        """Coinflip: 50/50 double or nothing"""
        won = random.choice([True, False])
        payout = bet * 2 if won else 0
        return won, payout

    @staticmethod
    def crash(bet: int) -> tuple[bool, int]:
        """Crash game: random multiplier"""
        mult = round(random.uniform(1.0, 10.0), 2)
        crash_point = round(random.uniform(1.1, 5.0), 2)
        
        if mult > crash_point:
            return False, 0  # Crashed before you cashed out
        else:
            payout = int(bet * mult)
            return True, payout, mult, crash_point

    @staticmethod
    def dice_roll(bet: int, prediction: str) -> tuple[bool, int]:
        """Dice: predict high or low"""
        d1, d2 = random.randint(1, 6), random.randint(1, 6)
        total = d1 + d2
        
        is_high = total >= 7
        won = (is_high and prediction == 'high') or (not is_high and prediction == 'low')
        
        payout = bet * 2 if won else 0
        return won, payout, d1, d2, total

# ===================== HANDLERS =====================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Start command"""
    users = UserManager.load_users()
    user = UserManager.get_or_create(update.effective_user.id, users)
    user['username'] = update.effective_user.first_name or 'Player'
    user['last_active'] = datetime.now().isoformat()
    UserManager.save_users(users)
    
    keyboard = [
        [InlineKeyboardButton("🎮 Play Games", callback_data='games_menu')],
        [InlineKeyboardButton("💰 Wallet", callback_data='wallet'), InlineKeyboardButton("📊 Stats", callback_data='stats')],
        [InlineKeyboardButton("🏆 Leaderboard", callback_data='leaderboard'), InlineKeyboardButton("🎁 Referral", callback_data='referral')],
        [InlineKeyboardButton("👤 Profile", callback_data='profile'), InlineKeyboardButton("⚙️ Settings", callback_data='settings')],
    ]
    
    if update.effective_user.id in ADMIN_IDS:
        keyboard.append([InlineKeyboardButton("🔧 Admin Panel", callback_data='admin_panel')])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        f"🎰 *Welcome to TokenArcade!*\n\n"
        f"Hi {user['username']}!\n\n"
        f"💰 Balance: `{user['coins']}¢`\n"
        f"🎁 Referral Code: `{user['ref_code']}`\n"
        f"⭐ VIP: {VIP_NAMES[UserManager.get_vip_level(user['xp'])]}\n\n"
        f"Choose an option below:",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )

async def games_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Games menu"""
    query = update.callback_query
    await query.answer()
    
    keyboard = [
        [InlineKeyboardButton("💣 Mines", callback_data='game_mines')],
        [InlineKeyboardButton("🎱 Keno", callback_data='game_keno')],
        [InlineKeyboardButton("🎰 Slots", callback_data='game_slots')],
        [InlineKeyboardButton("🎲 Dice", callback_data='game_dice')],
        [InlineKeyboardButton("💰 Coinflip", callback_data='game_coinflip')],
        [InlineKeyboardButton("📈 Crash", callback_data='game_crash')],
        [InlineKeyboardButton("🔙 Back", callback_data='back_to_main')],
    ]
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(
        "🎮 *Choose a Game*\n\n"
        "💣 Mines - Dodge bombs (1-25x)\n"
        "🎱 Keno - Match numbers (up to 50x)\n"
        "🎰 Slots - Spin to win (up to 100x)\n"
        "🎲 Dice - Predict high/low (2x)\n"
        "💰 Coinflip - 50/50 double (2x)\n"
        "📈 Crash - Beat the crash (up to 10x)\n",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )

async def game_mines(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Play Mines"""
    query = update.callback_query
    await query.answer()
    
    keyboard = [
        [InlineKeyboardButton("Bet 10", callback_data='mines_10')],
        [InlineKeyboardButton("Bet 50", callback_data='mines_50')],
        [InlineKeyboardButton("Bet 100", callback_data='mines_100')],
        [InlineKeyboardButton("Bet 500", callback_data='mines_500')],
        [InlineKeyboardButton("🔙 Back", callback_data='games_menu')],
    ]
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(
        "💣 *Mines Game*\n\n"
        "Dodge the bombs and escalate your multiplier!\n\n"
        "Select your bet:",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )

async def mines_play(update: Update, context: ContextTypes.DEFAULT_TYPE, bet: int) -> None:
    """Execute Mines game"""
    query = update.callback_query
    users = UserManager.load_users()
    user = UserManager.get_or_create(query.from_user.id, users)
    
    if bet > user['coins']:
        await query.answer(f"❌ Not enough coins! You have {user['coins']}", show_alert=True)
        return
    
    user['coins'] -= bet
    won, payout = GameEngine.mines(bet, 5)
    
    if won:
        user['coins'] += payout
        user['stats']['total_won'] += payout
        result_text = f"✅ *Won {payout}!*\n\nNew balance: `{user['coins']}`"
        if payout > user['stats']['biggest_win']:
            user['stats']['biggest_win'] = payout
    else:
        result_text = f"💥 *Boom! Lost {bet}*\n\nNew balance: `{user['coins']}`"
    
    user['stats']['games_played'] += 1
    user['stats']['total_wagered'] += bet
    user['xp'] += int(bet / 10)
    user['stats']['game_history']['mines'] = user['stats']['game_history'].get('mines', 0) + 1
    
    # VIP rakeback
    vip = UserManager.get_vip_level(user['xp'])
    rakeback = int(bet * VIP_RAKEBACK[vip])
    if rakeback > 0:
        user['coins'] += rakeback
        result_text += f"\n🎁 VIP Rakeback: +{rakeback}"
    
    UserManager.save_users(users)
    
    keyboard = [
        [InlineKeyboardButton("🔄 Play Again", callback_data='game_mines')],
        [InlineKeyboardButton("🎮 Other Games", callback_data='games_menu')],
        [InlineKeyboardButton("🏠 Main Menu", callback_data='back_to_main')],
    ]
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(
        result_text,
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )

async def wallet(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Wallet menu"""
    query = update.callback_query
    await query.answer()
    
    users = UserManager.load_users()
    user = UserManager.get_or_create(query.from_user.id, users)
    
    keyboard = [
        [InlineKeyboardButton("💸 Deposit", callback_data='deposit')],
        [InlineKeyboardButton("🏦 Withdraw", callback_data='withdraw')],
        [InlineKeyboardButton("📜 History", callback_data='wallet_history')],
        [InlineKeyboardButton("🔙 Back", callback_data='back_to_main')],
    ]
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(
        f"💰 *Wallet*\n\n"
        f"Balance: `{user['coins']}¢`\n"
        f"Total Deposited: `{user['wallet']['total_deposited']}`\n"
        f"Total Withdrawn: `{user['wallet']['total_withdrawn']}`\n"
        f"Pending Withdrawal: `{user['wallet']['pending_withdrawal']}`\n\n"
        f"Note: This is a demo. In production, connect to payment processor.",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """User stats"""
    query = update.callback_query
    await query.answer()
    
    users = UserManager.load_users()
    user = UserManager.get_or_create(query.from_user.id, users)
    vip = UserManager.get_vip_level(user['xp'])
    
    next_vip_xp = {
        VIPLevel.BRONZE: 1000,
        VIPLevel.SILVER: 5000,
        VIPLevel.GOLD: 15000,
        VIPLevel.PLATINUM: 50000,
        VIPLevel.DIAMOND: 999999
    }
    
    next_level = next_vip_xp.get(vip, 999999)
    xp_progress = user['xp']
    
    keyboard = [
        [InlineKeyboardButton("🔙 Back", callback_data='back_to_main')],
    ]
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(
        f"📊 *Your Stats*\n\n"
        f"💰 Balance: `{user['coins']}¢`\n"
        f"⭐ VIP Level: {VIP_NAMES[vip]} (⭐ x{VIP_MULTIPLIERS[vip]})\n"
        f"⚡ XP: `{xp_progress}` / `{next_level}`\n\n"
        f"🎮 Games Played: `{user['stats']['games_played']}`\n"
        f"💸 Total Wagered: `{user['stats']['total_wagered']}`\n"
        f"🏆 Total Won: `{user['stats']['total_won']}`\n"
        f"💎 Biggest Win: `{user['stats']['biggest_win']}`\n\n"
        f"🎁 Rakeback: {VIP_RAKEBACK[vip]*100}%",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )

async def leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Leaderboard"""
    query = update.callback_query
    await query.answer()
    
    users = UserManager.load_users()
    sorted_users = sorted(users.items(), key=lambda x: x[1]['coins'], reverse=True)[:10]
    
    leaderboard_text = "🏆 *Top 10 Players*\n\n"
    for i, (uid, u) in enumerate(sorted_users, 1):
        vip = UserManager.get_vip_level(u['xp'])
        leaderboard_text += f"{i}. {u.get('username', 'Player')} - `{u['coins']}¢` [{VIP_NAMES[vip]}]\n"
    
    keyboard = [
        [InlineKeyboardButton("🔙 Back", callback_data='back_to_main')],
    ]
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(
        leaderboard_text,
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )

async def referral(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Referral system"""
    query = update.callback_query
    await query.answer()
    
    users = UserManager.load_users()
    user = UserManager.get_or_create(query.from_user.id, users)
    
    ref_earnings = user['affiliate_earnings']
    ref_count = len(user['referrals'])
    
    keyboard = [
        [InlineKeyboardButton("📋 My Referrals", callback_data='my_referrals')],
        [InlineKeyboardButton("💰 Claim Earnings", callback_data='claim_referral_earnings')],
        [InlineKeyboardButton("🔙 Back", callback_data='back_to_main')],
    ]
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(
        f"🎁 *Referral System*\n\n"
        f"Your Code: `{user['ref_code']}`\n\n"
        f"📊 Referrals: `{ref_count}`\n"
        f"💰 Pending Earnings: `{ref_earnings}¢`\n\n"
        f"Share your code! You earn 10% commission on referral losses.",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )

async def profile(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """User profile"""
    query = update.callback_query
    await query.answer()
    
    users = UserManager.load_users()
    user = UserManager.get_or_create(query.from_user.id, users)
    vip = UserManager.get_vip_level(user['xp'])
    
    keyboard = [
        [InlineKeyboardButton("🔙 Back", callback_data='back_to_main')],
    ]
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(
        f"👤 *{user.get('username', 'Player')}*\n\n"
        f"🆔 UID: `{query.from_user.id}`\n"
        f"💰 Balance: `{user['coins']}¢`\n"
        f"⭐ VIP: {VIP_NAMES[vip]}\n"
        f"⚡ XP: `{user['xp']}`\n"
        f"🎁 Ref Code: `{user['ref_code']}`\n"
        f"📅 Joined: `{user['created_at'][:10]}`",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )

async def settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Settings"""
    query = update.callback_query
    await query.answer()
    
    users = UserManager.load_users()
    user = UserManager.get_or_create(query.from_user.id, users)
    
    notify_btn = "🔔 Notifications ON" if user['preferences']['notifications'] else "🔕 Notifications OFF"
    
    keyboard = [
        [InlineKeyboardButton(notify_btn, callback_data='toggle_notifications')],
        [InlineKeyboardButton("🗑️ Clear History", callback_data='clear_history')],
        [InlineKeyboardButton("🔙 Back", callback_data='back_to_main')],
    ]
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(
        "⚙️ *Settings*\n\n"
        "Customize your experience",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )

async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin panel"""
    query = update.callback_query
    
    if query.from_user.id not in ADMIN_IDS:
        await query.answer("❌ Unauthorized", show_alert=True)
        return
    
    await query.answer()
    
    users = UserManager.load_users()
    total_coins = sum(u['coins'] for u in users.values())
    total_users = len(users)
    total_games = sum(u['stats']['games_played'] for u in users.values())
    
    keyboard = [
        [InlineKeyboardButton("👥 Users", callback_data='admin_users')],
        [InlineKeyboardButton("💰 Wallet", callback_data='admin_wallet')],
        [InlineKeyboardButton("🔙 Back", callback_data='back_to_main')],
    ]
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(
        f"🔧 *Admin Panel*\n\n"
        f"👥 Total Users: `{total_users}`\n"
        f"💰 Total Coins: `{total_coins}¢`\n"
        f"🎮 Total Games Played: `{total_games}`\n",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )

async def back_to_main(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Back to main"""
    query = update.callback_query
    users = UserManager.load_users()
    user = UserManager.get_or_create(query.from_user.id, users)
    
    keyboard = [
        [InlineKeyboardButton("🎮 Play Games", callback_data='games_menu')],
        [InlineKeyboardButton("💰 Wallet", callback_data='wallet'), InlineKeyboardButton("📊 Stats", callback_data='stats')],
        [InlineKeyboardButton("🏆 Leaderboard", callback_data='leaderboard'), InlineKeyboardButton("🎁 Referral", callback_data='referral')],
        [InlineKeyboardButton("👤 Profile", callback_data='profile'), InlineKeyboardButton("⚙️ Settings", callback_data='settings')],
    ]
    
    if query.from_user.id in ADMIN_IDS:
        keyboard.append([InlineKeyboardButton("🔧 Admin Panel", callback_data='admin_panel')])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(
        f"🎰 *TokenArcade*\n\n"
        f"💰 Balance: `{user['coins']}¢`\n"
        f"⭐ VIP: {VIP_NAMES[UserManager.get_vip_level(user['xp'])]}\n\n"
        f"Choose an option:",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )

# ===================== CALLBACK HANDLER =====================

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Universal button handler"""
    query = update.callback_query
    data = query.data
    
    try:
        if data == 'games_menu':
            await games_menu(update, context)
        elif data == 'game_mines':
            await game_mines(update, context)
        elif data.startswith('mines_'):
            bet = int(data.split('_')[1])
            await mines_play(update, context, bet)
        elif data == 'wallet':
            await wallet(update, context)
        elif data == 'stats':
            await stats(update, context)
        elif data == 'leaderboard':
            await leaderboard(update, context)
        elif data == 'referral':
            await referral(update, context)
        elif data == 'profile':
            await profile(update, context)
        elif data == 'settings':
            await settings(update, context)
        elif data == 'admin_panel':
            await admin_panel(update, context)
        elif data == 'back_to_main':
            await back_to_main(update, context)
        else:
            await query.answer()
    except Exception as e:
        logger.error(f"Error in button handler: {e}", exc_info=True)
        await query.answer(f"❌ Error: {str(e)}", show_alert=True)

# ===================== INITIALIZATION =====================

async def init_bot() -> Application:
    """Initialize bot"""
    global bot_app
    
    logger.info("🔧 Initializing bot application...")
    
    bot_app = Application.builder().token(TOKEN).build()
    
    # Handlers
    bot_app.add_handler(CommandHandler("start", start))
    bot_app.add_handler(CallbackQueryHandler(button_handler))
    
    logger.info("✅ Bot initialized")
    return bot_app

def init_bot_sync() -> None:
    """Sync init wrapper"""
    global bot_app, loop
    
    if bot_app is None:
        bot_app = loop.run_until_complete(init_bot())

async def set_webhook_async() -> None:
    """Set webhook"""
    global bot_app
    
    if bot_app is None:
        logger.error("❌ Bot not initialized")
        return
    
    try:
        await bot_app.bot.set_webhook(url=f"{WEBHOOK_URL}/webhook")
        logger.info(f"✅ Webhook set: {WEBHOOK_URL}/webhook")
    except Exception as e:
        logger.error(f"❌ Webhook error: {e}")

def set_webhook_sync() -> None:
    """Sync webhook wrapper"""
    global loop
    loop.run_until_complete(set_webhook_async())

# ===================== FLASK ROUTES =====================

@flask_app.route('/', methods=['GET'])
def index():
    return '✅ TokenArcade Bot Running', 200

@flask_app.route('/webhook', methods=['POST'])
def webhook():
    """Webhook endpoint"""
    global bot_app, loop
    
    try:
        if bot_app is None:
            return 'Bot not ready', 503
        
        data = request.get_json()
        if not data:
            return '', 204
        
        update = Update.de_json(data, bot_app.bot)
        if not update:
            return '', 204
        
        loop.run_until_complete(bot_app.process_update(update))
        return '', 204
    
    except Exception as e:
        logger.error(f"❌ Webhook error: {e}", exc_info=True)
        return '', 500

# ===================== MAIN =====================

if __name__ == '__main__':
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    logger.info("🚀 Starting TokenArcade")
    logger.info(f"📱 Webhook: {WEBHOOK_URL}/webhook")
    
    # Initialize
    init_bot_sync()
    set_webhook_sync()
    
    # Run Flask
    logger.info(f"🚀 Flask running on 0.0.0.0:{PORT}")
    flask_app.run(
        host='0.0.0.0',
        port=PORT,
        debug=False,
        use_reloader=False,
        threaded=True
    )
