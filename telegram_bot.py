import os
import json
import logging
import asyncio
import time
import requests
from datetime import datetime
from typing import Optional

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
from flask import Flask, request

# ===================== LOGGING =====================
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ===================== CONFIG =====================
TOKEN = os.getenv('TELEGRAM_TOKEN')
WEBHOOK_URL = os.getenv('WEBHOOK_URL')
PORT = int(os.getenv('PORT', 8000))
DATA_FILE = 'arcade_users.json'

if not TOKEN or not WEBHOOK_URL:
    raise ValueError("❌ Missing TELEGRAM_TOKEN or WEBHOOK_URL")

logger.info(f"✅ Bot configured - Token: {TOKEN[:20]}... - Webhook: {WEBHOOK_URL}")

# ===================== FLASK =====================
flask_app = Flask(__name__)
bot_app: Optional[Application] = None
loop: Optional[asyncio.AbstractEventLoop] = None
FLASK_READY = False

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
        logger.info(f"🎯 START HANDLER CALLED - User: {update.effective_user.first_name} ({update.effective_user.id})")
        
        user = get_user(update.effective_user.id)
        logger.info(f"✅ User loaded: {user}")
        
        keyboard = [
            [InlineKeyboardButton("🎮 Play", callback_data='play')],
            [InlineKeyboardButton("💰 Balance", callback_data='balance')],
        ]
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        logger.info(f"📤 Sending reply...")
        await update.message.reply_text(
            f"🎰 *TokenArcade*\n\n"
            f"Hello {update.effective_user.first_name}!\n"
            f"Coins: `{user['coins']}`",
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )
        logger.info(f"✅ Reply sent!")
        
    except Exception as e:
        logger.error(f"❌ Error in start handler: {e}", exc_info=True)
        try:
            await update.message.reply_text(f"❌ Error: {str(e)}")
        except:
            pass

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
    logger.info(f"✅ Application created")
    
    bot_app.add_handler(CommandHandler("start", start))
    logger.info(f"✅ CommandHandler for /start added")
    
    bot_app.add_handler(CallbackQueryHandler(button))
    logger.info(f"✅ CallbackQueryHandler added")
    
    logger.info("✅ All handlers registered")
    return bot_app

def init_bot_sync():
    global bot_app, loop
    if bot_app is None:
        logger.info("Running async init...")
        bot_app = loop.run_until_complete(init_bot())

async def set_webhook_async():
    global bot_app
    try:
        webhook_url = f"{WEBHOOK_URL}/webhook"
        logger.info(f"🔗 Setting webhook to {webhook_url}")
        result = await bot_app.bot.set_webhook(url=webhook_url)
        logger.info(f"✅ Webhook set: {result}")
        
        info = await bot_app.bot.get_webhook_info()
        logger.info(f"✅ Webhook info: {info}")
        
    except Exception as e:
        logger.error(f"❌ Webhook error: {e}", exc_info=True)

def set_webhook_sync():
    global loop
    try:
        loop.run_until_complete(set_webhook_async())
    except Exception as e:
        logger.error(f"❌ Error setting webhook: {e}", exc_info=True)

# ===================== FLASK =====================

@flask_app.route('/', methods=['GET', 'HEAD'])
def index():
    logger.debug("📍 GET / - Health check")
    return '✅ Bot Ready', 200

@flask_app.route('/webhook', methods=['POST'])
def webhook():
    """Webhook endpoint"""
    global bot_app, loop
    
    logger.info("=" * 80)
    logger.info("🔔 WEBHOOK RECEIVED")
    
    try:
        if bot_app is None:
            logger.error("❌ bot_app is None!")
            return 'Bot not ready', 503
        
        data = request.get_json()
        logger.info(f"📨 Data received: {json.dumps(data)[:100]}...")
        
        if not data:
            logger.warning("⚠️  Empty JSON")
            return '', 204
        
        from telegram import Update
        update = Update.de_json(data, bot_app.bot)
        
        if not update:
            logger.warning("⚠️  Update is None")
            return '', 204
        
        logger.info(f"🔄 Processing update...")
        loop.run_until_complete(bot_app.process_update(update))
        logger.info(f"✅ Update processed!")
        
        return '', 204
    
    except Exception as e:
        logger.error(f"❌ WEBHOOK ERROR: {e}", exc_info=True)
        return '', 500
    
    finally:
        logger.info("=" * 80)

# ===================== MAIN =====================

if __name__ == '__main__':
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    logger.info("=" * 80)
    logger.info("🚀 STARTING TOKENARCADE BOT")
    logger.info("=" * 80)
    logger.info(f"Token: {TOKEN[:20]}...")
    logger.info(f"Webhook URL: {WEBHOOK_URL}/webhook")
    logger.info(f"Port: {PORT}")
    
    logger.info("\n🔧 Initializing bot...")
    init_bot_sync()
    
    logger.info(f"\n🌍 Starting Flask on 0.0.0.0:{PORT}")
    logger.info("⏳ Starting Flask server...")
    
    # Import threading AFTER app is created
    import threading
    
    def wait_and_set_webhook():
        """Wait for Flask to be ready, then set webhook"""
        logger.info("⏳ Waiting for Flask to be ready (checking endpoint)...")
        
        # Wait up to 30 seconds for Flask to be ready
        for attempt in range(30):
            try:
                # Try to reach the health check endpoint
                response = requests.get(f"http://localhost:{PORT}/", timeout=2)
                if response.status_code == 200:
                    logger.info(f"✅ Flask is ready! (attempt {attempt + 1})")
                    time.sleep(2)  # Extra safety wait
                    logger.info("\n🔗 Now setting webhook...")
                    set_webhook_sync()
                    return
            except Exception as e:
                logger.debug(f"Flask not ready yet ({attempt + 1}/30): {e}")
                time.sleep(1)
        
        logger.error("❌ Flask didn't start in time!")
    
    # Start webhook setup in background
    webhook_thread = threading.Thread(target=wait_and_set_webhook, daemon=True)
    webhook_thread.start()
    
    # Run Flask (blocking)
    logger.info("▶️  Flask running...\n")
    flask_app.run(
        host='0.0.0.0',
        port=PORT,
        debug=False,
        use_reloader=False,
        threaded=True
    )
