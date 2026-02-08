import sqlite3
import os
import json
import secrets
import asyncio
import pytz
from datetime import datetime, timedelta
from aiohttp import web
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ChatMemberHandler, filters, ContextTypes

# Database setup
DB_DIR = os.path.expanduser("~/light_status_data")
os.makedirs(DB_DIR, exist_ok=True)
DB_FILE = os.path.join(DB_DIR, "config.db")

# Configuration
TIMEOUT_MINUTES = 5
HTTP_PORT = 8080

def init_db():
    conn = sqlite3.connect(DB_FILE)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS channels (
            channel_id INTEGER PRIMARY KEY,
            owner_id INTEGER,
            api_key TEXT UNIQUE,
            timezone TEXT DEFAULT 'Europe/Kiev',
            last_request_time REAL,
            is_power_on INTEGER DEFAULT 0,
            last_status_change REAL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS user_sessions (
            user_id INTEGER PRIMARY KEY,
            active_channel_id INTEGER
        )
    """)
    conn.commit()
    conn.close()

def get_channel_by_key(api_key):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.execute("SELECT channel_id, timezone, last_request_time, is_power_on, last_status_change FROM channels WHERE api_key = ?", (api_key,))
    row = cur.fetchone()
    conn.close()
    if row:
        return {
            "channel_id": row[0],
            "timezone": row[1],
            "last_request_time": row[2],
            "is_power_on": bool(row[3]),
            "last_status_change": row[4]
        }
    return None

def update_last_request(api_key, timestamp):
    conn = sqlite3.connect(DB_FILE)
    conn.execute("UPDATE channels SET last_request_time = ? WHERE api_key = ?", (timestamp, api_key))
    conn.commit()
    conn.close()

def update_power_status(api_key, is_on, timestamp):
    conn = sqlite3.connect(DB_FILE)
    conn.execute("UPDATE channels SET is_power_on = ?, last_status_change = ? WHERE api_key = ?", 
                 (1 if is_on else 0, timestamp, api_key))
    conn.commit()
    conn.close()

def get_channel_config(channel_id):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.execute("SELECT owner_id, api_key, timezone, last_request_time, is_power_on, last_status_change FROM channels WHERE channel_id = ?", (channel_id,))
    row = cur.fetchone()
    conn.close()
    if row:
        return {
            "owner_id": row[0],
            "api_key": row[1],
            "timezone": row[2],
            "last_request_time": row[3],
            "is_power_on": bool(row[4]),
            "last_status_change": row[5]
        }
    return {"owner_id": None, "api_key": None, "timezone": "Europe/Kiev", "last_request_time": None, "is_power_on": False, "last_status_change": None}

def create_channel(channel_id, owner_id):
    api_key = secrets.token_urlsafe(16)
    conn = sqlite3.connect(DB_FILE)
    try:
        conn.execute("INSERT INTO channels (channel_id, owner_id, api_key) VALUES (?, ?, ?)", 
                     (channel_id, owner_id, api_key))
        conn.commit()
        return api_key
    except sqlite3.IntegrityError:
        return None
    finally:
        conn.close()

def is_owner(channel_id, user_id):
    config = get_channel_config(channel_id)
    return config["owner_id"] is None or config["owner_id"] == user_id

def set_user_active_channel(user_id, channel_id):
    conn = sqlite3.connect(DB_FILE)
    conn.execute("INSERT OR REPLACE INTO user_sessions (user_id, active_channel_id) VALUES (?, ?)", 
                 (user_id, channel_id))
    conn.commit()
    conn.close()

def get_user_active_channel(user_id):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.execute("SELECT active_channel_id FROM user_sessions WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else None

def set_timezone(channel_id, tz):
    conn = sqlite3.connect(DB_FILE)
    conn.execute("UPDATE channels SET timezone = ? WHERE channel_id = ?", (tz, channel_id))
    conn.commit()
    conn.close()

def format_duration(seconds):
    """Format duration in Ukrainian"""
    if seconds < 60:
        return f"{int(seconds)}с"
    elif seconds < 3600:
        mins = int(seconds / 60)
        secs = int(seconds % 60)
        return f"{mins}хв {secs}с" if secs > 0 else f"{mins}хв"
    else:
        hours = int(seconds / 3600)
        mins = int((seconds % 3600) / 60)
        return f"{hours}год {mins}хв" if mins > 0 else f"{hours}год"

# Telegram bot commands
def get_channel_id_from_arg(arg):
    """Convert channel username or ID to channel ID"""
    if arg.startswith('@'):
        # Username - we'll need to resolve it
        # For now, return None and let Telegram API handle it
        return None
    try:
        return int(arg)
    except ValueError:
        return None

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Команди:\n"
        "/create_channel <channel_id> - створити новий канал (генерує ключ)\n"
        "/import_channel <channel_id> <key> - імпортувати з існуючим ключем\n"
        "/get_key <channel_id> - отримати API ключ\n"
        "/set_timezone <channel_id> <timezone> - встановити часовий пояс\n"
        "/status <channel_id> - перевірити статус\n\n"
        "Перешліть повідомлення з каналу для отримання ID."
    )

async def create_channel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Використання: /create_channel <channel_id>")
        return
    
    try:
        channel_id = int(context.args[0])
        user_id = update.message.from_user.id
        
        config = get_channel_config(channel_id)
        if config["owner_id"] is not None:
            await update.message.reply_text("❌ Цей канал вже налаштований")
            return
        
        api_key = create_channel(channel_id, user_id)
        if api_key:
            await update.message.reply_text(
                f"✅ Канал створено!\n\n"
                f"🔑 API ключ: `{api_key}`\n\n"
                f"Використовуйте:\n"
                f"`curl http://YOUR_SERVER:{HTTP_PORT}/channelPing?channel_key={api_key}`"
            )
        else:
            await update.message.reply_text("❌ Помилка створення каналу")
    except ValueError:
        await update.message.reply_text("❌ Невірний ID каналу")

async def import_channel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        await update.message.reply_text("Використання: /import_channel <channel_id> <api_key>")
        return
    
    try:
        channel_id = int(context.args[0])
        api_key = context.args[1]
        user_id = update.message.from_user.id
        
        config = get_channel_config(channel_id)
        if config["owner_id"] is not None:
            await update.message.reply_text("❌ Цей канал вже налаштований")
            return
        
        # Create channel with provided key
        conn = sqlite3.connect(DB_FILE)
        try:
            conn.execute("INSERT INTO channels (channel_id, owner_id, api_key) VALUES (?, ?, ?)", 
                         (channel_id, user_id, api_key))
            conn.commit()
            await update.message.reply_text(
                f"✅ Канал імпортовано!\n\n"
                f"🔑 API ключ: `{api_key}`\n\n"
                f"Додайте до вашого скрипту:\n"
                f"`curl http://YOUR_SERVER:{HTTP_PORT}/channelPing?channel_key={api_key}`"
            )
        except sqlite3.IntegrityError:
            await update.message.reply_text("❌ Цей ключ вже використовується")
        finally:
            conn.close()
    except ValueError:
        await update.message.reply_text("❌ Невірний ID каналу")

async def get_key_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Використання: /get_key <channel_id>")
        return
    
    try:
        channel_id = int(context.args[0])
        user_id = update.message.from_user.id
        
        if not is_owner(channel_id, user_id):
            await update.message.reply_text("❌ Ви не є власником цього каналу")
            return
        
        config = get_channel_config(channel_id)
        if config["owner_id"] is None:
            await update.message.reply_text("❌ Канал не налаштований")
            return
        
        await update.message.reply_text(
            f"🔑 API ключ: `{config['api_key']}`\n\n"
            f"Використовуйте:\n"
            f"`curl http://YOUR_SERVER:{HTTP_PORT}/channelPing?channel_key={config['api_key']}`"
        )
    except ValueError:
        await update.message.reply_text("❌ Невірний ID каналу")

async def set_timezone_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        await update.message.reply_text(
            "Використання: /set_timezone <channel_id> <timezone>\n\n"
            "Приклади:\n"
            "/set_timezone -1001234567890 Europe/Kiev\n"
            "/set_timezone -1001234567890 Europe/Warsaw\n"
            "/set_timezone -1001234567890 America/New_York\n\n"
            "Повний список: https://en.wikipedia.org/wiki/List_of_tz_database_time_zones"
        )
        return
    
    try:
        channel_id = int(context.args[0])
        tz = context.args[1]
        user_id = update.message.from_user.id
        
        if not is_owner(channel_id, user_id):
            await update.message.reply_text("❌ Ви не є власником цього каналу")
            return
        
        config = get_channel_config(channel_id)
        if config["owner_id"] is None:
            await update.message.reply_text("❌ Канал не налаштований")
            return
        
        if tz not in pytz.all_timezones:
            await update.message.reply_text("❌ Невірний часовий пояс")
            return
        
        set_timezone(channel_id, tz)
        await update.message.reply_text(f"✅ Часовий пояс встановлено: {tz}")
    except ValueError:
        await update.message.reply_text("❌ Невірний ID каналу")

async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    
    if not context.args:
        # Show all channels
        conn = sqlite3.connect(DB_FILE)
        channels = conn.execute("SELECT channel_id FROM channels WHERE owner_id = ?", (user_id,)).fetchall()
        conn.close()
        
        if not channels:
            await update.message.reply_text("❌ У вас немає налаштованих каналів")
            return
        
        msg = "📊 Ваші канали:\n\n"
        for (channel_id,) in channels:
            config = get_channel_config(channel_id)
            if config["last_request_time"] is None:
                msg += f"{channel_id}: 🔴 (немає запитів)\n"
            else:
                tz = pytz.timezone(config["timezone"])
                now = datetime.now(tz).timestamp()
                time_since = now - config["last_request_time"]
                status_emoji = "🟢" if config["is_power_on"] else "🔴"
                msg += f"{channel_id}: {status_emoji} ({format_duration(time_since)} тому)\n"
        
        await update.message.reply_text(msg)
        return
    
    try:
        channel_id = int(context.args[0])
        
        if not is_owner(channel_id, user_id):
            await update.message.reply_text("❌ Ви не є власником цього каналу")
            return
        
        config = get_channel_config(channel_id)
        if config["owner_id"] is None:
            await update.message.reply_text("❌ Канал не налаштований")
            return
        
        if config["last_request_time"] is None:
            await update.message.reply_text("📊 Статус: 🔴 світла немає\n\n⚠️ Ще не було жодного запиту")
            return
        
        tz = pytz.timezone(config["timezone"])
        now = datetime.now(tz).timestamp()
        last_req = config["last_request_time"]
        time_since = now - last_req
        
        status_emoji = "🟢" if config["is_power_on"] else "🔴"
        status_text = "світло є" if config["is_power_on"] else "світла немає"
        
        msg = f"📊 Статус: {status_emoji} {status_text}\n\n"
        msg += f"📶 Останній запит: {format_duration(time_since)} тому\n"
        
        if config["last_status_change"]:
            status_duration = now - config["last_status_change"]
            msg += f"🔄 Статус змінено: {format_duration(status_duration)} тому"
        
        await update.message.reply_text(msg)
    except ValueError:
        await update.message.reply_text("❌ Невірний ID каналу")

async def handle_forwarded(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg:
        return
    
    if hasattr(msg, 'forward_origin') and msg.forward_origin:
        origin = msg.forward_origin
        if hasattr(origin, 'chat') and origin.chat and origin.chat.type == "channel":
            channel_id = origin.chat.id
            await msg.reply_text(
                f"ID каналу: {channel_id}\n\n"
                f"Використайте: /create_channel {channel_id}"
            )

async def handle_my_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle bot being added to channel"""
    if not update.my_chat_member:
        return
    
    chat = update.my_chat_member.chat
    if chat.type != "channel":
        return
    
    new_status = update.my_chat_member.new_chat_member.status
    
    # Bot was added to channel
    if new_status in ["administrator", "member"]:
        channel_id = chat.id
        config = get_channel_config(channel_id)
        
        # Only post if channel is configured
        if config["owner_id"] is not None:
            tz = pytz.timezone(config["timezone"])
            now = datetime.now(tz)
            time_str = now.strftime("%H:%M")
            
            # Check current status
            if config["last_request_time"] is None:
                # No requests yet - assume offline
                message = f"🔴 {time_str} Світло зникло\n🕓 Статус невідомий (бот щойно доданий)"
            else:
                now_ts = now.timestamp()
                time_since = now_ts - config["last_request_time"]
                timeout_seconds = TIMEOUT_MINUTES * 60
                
                if time_since > timeout_seconds:
                    # Offline
                    message = f"🔴 {time_str} Світло зникло\n🕓 Останній запит: {format_duration(time_since)} тому"
                else:
                    # Online
                    message = f"🟢 {time_str} Світло є\n🕓 Останній запит: {format_duration(time_since)} тому"
            
            try:
                await context.bot.send_message(chat_id=channel_id, text=message)
            except Exception as e:
                print(f"Error sending initial status to {channel_id}: {e}")

# HTTP server for ping requests
telegram_app = None

async def handle_ping(request):
    api_key = request.query.get('channel_key')
    if not api_key:
        return web.Response(text="Missing channel_key parameter", status=400)
    
    channel = get_channel_by_key(api_key)
    if not channel:
        return web.Response(text="Invalid key", status=403)
    
    now = datetime.now().timestamp()
    was_on = channel["is_power_on"]
    
    # Update last request time
    update_last_request(api_key, now)
    
    # If power was off, turn it on and send message
    if not was_on:
        update_power_status(api_key, True, now)
        
        # Calculate how long it was off
        if channel["last_status_change"]:
            duration = now - channel["last_status_change"]
            duration_text = format_duration(duration)
        else:
            duration_text = "невідомо"
        
        # Send Telegram message
        tz = pytz.timezone(channel["timezone"])
        time_str = datetime.fromtimestamp(now, tz).strftime("%H:%M")
        
        message = f"🟢 {time_str} Світло з'явилося\n🕓 Його не було {duration_text}"
        
        if telegram_app:
            await telegram_app.bot.send_message(
                chat_id=channel["channel_id"],
                text=message
            )
    
    return web.Response(text="OK")

async def check_timeouts():
    """Background task to check for timeouts"""
    while True:
        await asyncio.sleep(30)  # Check every 30 seconds
        
        conn = sqlite3.connect(DB_FILE)
        cur = conn.execute("SELECT channel_id, api_key, timezone, last_request_time, is_power_on, last_status_change FROM channels WHERE is_power_on = 1")
        channels = cur.fetchall()
        conn.close()
        
        now = datetime.now().timestamp()
        timeout_seconds = TIMEOUT_MINUTES * 60
        
        for row in channels:
            channel_id, api_key, tz_str, last_req, is_on, last_change = row
            
            if last_req and (now - last_req) > timeout_seconds:
                # Power is off
                update_power_status(api_key, False, now)
                
                # Calculate how long it was on
                if last_change:
                    duration = now - last_change
                    duration_text = format_duration(duration)
                else:
                    duration_text = "невідомо"
                
                # Send Telegram message
                tz = pytz.timezone(tz_str)
                time_str = datetime.fromtimestamp(last_req, tz).strftime("%H:%M")
                
                message = f"🔴 {time_str} Світло зникло\n🕓 Воно було {duration_text}"
                
                if telegram_app:
                    try:
                        await telegram_app.bot.send_message(
                            chat_id=channel_id,
                            text=message
                        )
                    except Exception as e:
                        print(f"Error sending message to {channel_id}: {e}")

def main():
    global telegram_app
    
    init_db()
    
    # Get bot token
    import os
    token = os.getenv("BOT_TOKEN")
    if not token:
        try:
            with open("token.txt") as f:
                token = f.read().strip()
        except FileNotFoundError:
            print("ERROR: BOT_TOKEN environment variable not set and token.txt not found")
            return
    
    # Create Telegram bot
    telegram_app = Application.builder().token(token).build()
    
    telegram_app.add_handler(CommandHandler("start", start))
    telegram_app.add_handler(CommandHandler("create_channel", create_channel_cmd))
    telegram_app.add_handler(CommandHandler("import_channel", import_channel_cmd))
    telegram_app.add_handler(CommandHandler("get_key", get_key_cmd))
    telegram_app.add_handler(CommandHandler("set_timezone", set_timezone_cmd))
    telegram_app.add_handler(CommandHandler("status", status_cmd))
    telegram_app.add_handler(MessageHandler(filters.FORWARDED & filters.ChatType.PRIVATE, handle_forwarded))
    telegram_app.add_handler(ChatMemberHandler(handle_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))
    
    # Start HTTP server
    app = web.Application()
    app.router.add_get('/channelPing', handle_ping)
    
    # Run both servers
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    # Start timeout checker
    loop.create_task(check_timeouts())
    
    # Start HTTP server
    runner = web.AppRunner(app)
    loop.run_until_complete(runner.setup())
    site = web.TCPSite(runner, '0.0.0.0', HTTP_PORT)
    loop.run_until_complete(site.start())
    
    print(f"HTTP server started on port {HTTP_PORT}")
    print("Starting Telegram bot...")
    
    # Start Telegram bot
    telegram_app.run_polling()

if __name__ == "__main__":
    main()
