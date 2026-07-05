import os
import json
import logging
import asyncio
import time
import threading
import requests
from datetime import datetime
from typing import Optional

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
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
DATA_FILE = 'arcade_users.json'

if not TOKEN or not WEBHOOK_URL:
    raise ValueError("❌ Missing TELEGRAM_TOKEN or WEBHOOK_URL")

logger.info(f"✅ Bot configured - Webhook: {WEBHOOK_URL}")

# ===================== FLASK =====================
flask_app = Flask(__name__)
bot_app: Optional[Application] = None
loop: Optional[asyncio.AbstractEventLoop] = None
loop_lock = threading.Lock()

# ===================== SIMPLE STORAGE =====================
def load_users():
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, 'r') as f:
                return json.load(f)
        except:
            pass
    return {}

def save_users(users):
    try:
        with open(DATA_FILE, 'w') as f:
            json.dump(users, f, indent=2)
    except:
        pass

def get_user(user_id):
    users = load_users()
    uid = str(user_id)
    if uid not in users:
        users[uid] = {
            'coins': 1000,
            'username': '',
            'created_at': datetime.now().isoformat()
        }
        save_users(users)
    return users[uid]

# ===================== HANDLERS =====================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Start command"""
    try:
        logger.info(f"🎯 /start from {update.effective_user.first_name} ({update.effective_user.id})")
        
        user = get_user(update.effective_user.id)
        
        keyboard = [
            [InlineKeyboardButton("🎮 Play", callback_data='play')],
            [InlineKeyboardButton("💰 Balance", callback_data='balance')],
        ]
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(
            f"🎰 *TokenArcade*\n\n"
            f"Hello {update.effective_user.first_name}!\n"
            f"Coins: `{user['coins']}`",
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )
        logger.info(f"✅ Reply sent to {update.effective_user.first_name}!")
        
    except Exception as e:
        logger.error(f"❌ Error in start handler: {e}", exc_info=True)

async def button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Button handler"""
    query = update.callback_query
    logger.info(f"🔘 Button pressed: {query.data}")
    await query.answer()
    await query.edit_message_text(f"You pressed: {query.data}")

# ===================== INITIALIZATION =====================

async def init_bot() -> Application:
    """Initialize bot"""
    global bot_app
    logger.info("🔧 Initializing application...")
    
    bot_app = Application.builder().token(TOKEN).build()
    bot_app.add_handler(CommandHandler("start", start))
    bot_app.add_handler(CallbackQueryHandler(button))
    
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
        logger.info(f"🔗 Clearing old webhook + pending updates...")
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

# ===================== FLASK =====================

@flask_app.route('/', methods=['GET', 'HEAD'])
def index():
    return '✅ Bot Ready', 200

@flask_app.route('/webhook', methods=['POST'])
def webhook():
    """Webhook endpoint - thread-safe using lock"""
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
        
        logger.info(f"🔔 Incoming update {update.update_id}")
        
        with loop_lock:
            loop.run_until_complete(bot_app.process_update(update))
        
        logger.info(f"✅ Update {update.update_id} processed")
        return '', 200
    
    except Exception as e:
        logger.error(f"❌ WEBHOOK ERROR: {e}", exc_info=True)
        return '', 200

# ===================== MAIN =====================

if __name__ == '__main__':
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    logger.info("🚀 Starting TokenArcade Bot")
    
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
    
    webhook_thread = threading.Thread(target=wait_and_setup_webhook, daemon=True)
    webhook_thread.start()
    
    logger.info(f"🌍 Flask starting on 0.0.0.0:{PORT}")
    flask_app.run(
        host='0.0.0.0',
        port=PORT,
        debug=False,
        use_reloader=False,
        threaded=False
    )
