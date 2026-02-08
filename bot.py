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
            last_status_change REAL,
            paused INTEGER DEFAULT 0
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            channel_id INTEGER,
            status INTEGER,
            timestamp REAL,
            FOREIGN KEY (channel_id) REFERENCES channels(channel_id) ON DELETE CASCADE
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS notifications (
            user_id INTEGER,
            channel_id INTEGER,
            enabled INTEGER DEFAULT 1,
            PRIMARY KEY (user_id, channel_id),
            FOREIGN KEY (channel_id) REFERENCES channels(channel_id) ON DELETE CASCADE
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
    cur = conn.execute("SELECT channel_id FROM channels WHERE api_key = ?", (api_key,))
    row = cur.fetchone()
    if row:
        channel_id = row[0]
        conn.execute("UPDATE channels SET is_power_on = ?, last_status_change = ? WHERE api_key = ?", 
                     (1 if is_on else 0, timestamp, api_key))
        conn.execute("INSERT INTO history (channel_id, status, timestamp) VALUES (?, ?, ?)",
                     (channel_id, 1 if is_on else 0, timestamp))
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

async def resolve_channel_id(context: ContextTypes.DEFAULT_TYPE, channel_input: str):
    """Resolve channel username or ID to numeric channel_id"""
    if channel_input.startswith('@'):
        # Try to get chat info by username
        try:
            chat = await context.bot.get_chat(channel_input)
            return chat.id
        except Exception:
            return None
    else:
        # Already numeric ID
        try:
            return int(channel_input)
        except ValueError:
            return None

def set_timezone(channel_id, tz):
    conn = sqlite3.connect(DB_FILE)
    conn.execute("UPDATE channels SET timezone = ? WHERE channel_id = ?", (tz, channel_id))
    conn.commit()
    conn.close()

def get_daily_stats(channel_id, timezone):
    """Calculate today's uptime, downtime, and outage count"""
    tz = pytz.timezone(timezone)
    now = datetime.now(tz)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    now_ts = now.timestamp()
    
    conn = sqlite3.connect(DB_FILE)
    rows = conn.execute(
        "SELECT status, timestamp FROM history WHERE channel_id = ? AND timestamp >= ? ORDER BY timestamp ASC",
        (channel_id, today_start)
    ).fetchall()
    conn.close()
    
    if not rows:
        return None
    
    uptime = 0
    downtime = 0
    outages = 0
    
    # Start counting from FIRST event today, not from midnight
    prev_status = rows[0][0]
    prev_time = rows[0][1]  # Use first event time, not today_start
    
    for status, timestamp in rows:
        duration = timestamp - prev_time
        if prev_status == 1:
            uptime += duration
        else:
            downtime += duration
        
        if status == 0 and prev_status == 1:
            outages += 1
        
        prev_status = status
        prev_time = timestamp
    
    # Add time from last event to now
    duration = now_ts - prev_time
    if prev_status == 1:
        uptime += duration
    else:
        downtime += duration
    
    return {
        "uptime": uptime,
        "downtime": downtime,
        "outages": outages
    }

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
        "/create_channel <channel_id|@username> - створити новий канал\n"
        "/import_channel <channel_id|@username> <key> - імпортувати з ключем\n"
        "/get_key <channel_id|@username> - отримати API ключ\n"
        "/set_timezone <channel_id|@username> <timezone> - встановити часовий пояс\n"
        "/regenerate_key <channel_id|@username> - згенерувати новий ключ\n"
        "/replace_key <channel_id|@username> <key> - замінити ключ\n"
        "/remove_channel <channel_id|@username> - видалити канал\n"
        "/transfer <channel_id|@username> <user_id> - передати власність\n"
        "/history <channel_id|@username> [кількість] - історія змін\n"
        "/notify <channel_id|@username> <on|off> - сповіщення в DM\n"
        "/notify - показати налаштування сповіщень\n"
        "/pause <channel_id|@username> <on|off> - призупинити/відновити\n"
        "/stop <channel_id|@username> - зупинити моніторинг\n"
        "/resume <channel_id|@username> - відновити моніторинг\n"
        "/export <channel_id|@username> <csv|json> - експорт всієї історії\n"
        "/status <channel_id|@username> - перевірити статус\n"
        "/status - показати всі канали\n\n"
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
    
    channel_id = await resolve_channel_id(context, context.args[0])
    if channel_id is None:
        await update.message.reply_text("❌ Невірний ID або username каналу")
        return
    
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

async def regenerate_key_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Використання: /regenerate_key <channel_id|@username>")
        return
    
    channel_id = await resolve_channel_id(context, context.args[0])
    if channel_id is None:
        await update.message.reply_text("❌ Невірний ID або username каналу")
        return
    
    user_id = update.message.from_user.id
    
    if not is_owner(channel_id, user_id):
        await update.message.reply_text("❌ Ви не є власником цього каналу")
        return
    
    config = get_channel_config(channel_id)
    if config["owner_id"] is None:
        await update.message.reply_text("❌ Канал не налаштований")
        return
    
    new_key = secrets.token_urlsafe(32)
    conn = sqlite3.connect(DB_FILE)
    conn.execute("UPDATE channels SET api_key = ? WHERE channel_id = ?", (new_key, channel_id))
    conn.commit()
    conn.close()
    
    await update.message.reply_text(
        f"✅ Новий API ключ згенеровано!\n\n"
        f"🔑 API ключ: `{new_key}`\n\n"
        f"⚠️ Старий ключ більше не працює. Оновіть його у вашому скрипті:\n"
        f"`curl http://YOUR_SERVER:{HTTP_PORT}/channelPing?channel_key={new_key}`"
    )

async def replace_key_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        await update.message.reply_text("Використання: /replace_key <channel_id|@username> <new_key>")
        return
    
    channel_id = await resolve_channel_id(context, context.args[0])
    if channel_id is None:
        await update.message.reply_text("❌ Невірний ID або username каналу")
        return
    
    new_key = context.args[1]
    user_id = update.message.from_user.id
    
    if not is_owner(channel_id, user_id):
        await update.message.reply_text("❌ Ви не є власником цього каналу")
        return
    
    config = get_channel_config(channel_id)
    if config["owner_id"] is None:
        await update.message.reply_text("❌ Канал не налаштований")
        return
    
    conn = sqlite3.connect(DB_FILE)
    conn.execute("UPDATE channels SET api_key = ? WHERE channel_id = ?", (new_key, channel_id))
    conn.commit()
    conn.close()
    
    await update.message.reply_text(
        f"✅ API ключ замінено!\n\n"
        f"🔑 Новий ключ: `{new_key}`\n\n"
        f"⚠️ Старий ключ більше не працює."
    )

async def remove_channel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Використання: /remove_channel <channel_id|@username>")
        return
    
    channel_id = await resolve_channel_id(context, context.args[0])
    if channel_id is None:
        await update.message.reply_text("❌ Невірний ID або username каналу")
        return
    
    user_id = update.message.from_user.id
    
    if not is_owner(channel_id, user_id):
        await update.message.reply_text("❌ Ви не є власником цього каналу")
        return
    
    config = get_channel_config(channel_id)
    if config["owner_id"] is None:
        await update.message.reply_text("❌ Канал не налаштований")
        return
    
    conn = sqlite3.connect(DB_FILE)
    conn.execute("DELETE FROM channels WHERE channel_id = ?", (channel_id,))
    conn.commit()
    conn.close()
    
    await update.message.reply_text("✅ Канал видалено")

async def transfer_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        await update.message.reply_text("Використання: /transfer <channel_id|@username> <new_owner_user_id>")
        return
    
    channel_id = await resolve_channel_id(context, context.args[0])
    if channel_id is None:
        await update.message.reply_text("❌ Невірний ID або username каналу")
        return
    
    try:
        new_owner_id = int(context.args[1])
    except (ValueError, IndexError):
        await update.message.reply_text("❌ Невірний ID користувача")
        return
    
    user_id = update.message.from_user.id
    
    if not is_owner(channel_id, user_id):
        await update.message.reply_text("❌ Ви не є власником цього каналу")
        return
    
    config = get_channel_config(channel_id)
    if config["owner_id"] is None:
        await update.message.reply_text("❌ Канал не налаштований")
        return
    
    conn = sqlite3.connect(DB_FILE)
    conn.execute("UPDATE channels SET owner_id = ? WHERE channel_id = ?", (new_owner_id, channel_id))
    conn.commit()
    conn.close()
    
    await update.message.reply_text(f"✅ Власника каналу передано користувачу {new_owner_id}")

async def history_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Використання: /history <channel_id|@username> [кількість]")
        return
    
    channel_id = await resolve_channel_id(context, context.args[0])
    if channel_id is None:
        await update.message.reply_text("❌ Невірний ID або username каналу")
        return
    
    try:
        limit = int(context.args[1]) if len(context.args) > 1 else 10
    except ValueError:
        await update.message.reply_text("❌ Невірна кількість")
        return
    
    user_id = update.message.from_user.id
    
    if not is_owner(channel_id, user_id):
        await update.message.reply_text("❌ Ви не є власником цього каналу")
        return
    
    config = get_channel_config(channel_id)
    if config["owner_id"] is None:
        await update.message.reply_text("❌ Канал не налаштований")
        return
    
    conn = sqlite3.connect(DB_FILE)
    rows = conn.execute(
        "SELECT status, timestamp FROM history WHERE channel_id = ? ORDER BY timestamp DESC LIMIT ?",
        (channel_id, limit)
    ).fetchall()
    conn.close()
    
    if not rows:
        await update.message.reply_text("📜 Історія порожня")
        return
    
    tz = pytz.timezone(config["timezone"])
    msg = f"📜 Історія (останні {len(rows)}):\n\n"
    
    prev_timestamp = None
    for status, timestamp in rows:
        dt = datetime.fromtimestamp(timestamp, tz)
        status_emoji = "🟢" if status == 1 else "🔴"
        status_text = "з'явилося" if status == 1 else "зникло"
        
        duration_text = ""
        if prev_timestamp:
            duration = prev_timestamp - timestamp
            duration_text = f" (тривало {format_duration(duration)})"
        
        msg += f"{status_emoji} {dt.strftime('%d.%m %H:%M')} Світло {status_text}{duration_text}\n"
        prev_timestamp = timestamp
    
    await update.message.reply_text(msg)

async def notify_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    
    if not context.args:
        # Show notification settings for all channels
        conn = sqlite3.connect(DB_FILE)
        channels = conn.execute("SELECT channel_id FROM channels WHERE owner_id = ?", (user_id,)).fetchall()
        notifications = conn.execute("SELECT channel_id FROM notifications WHERE user_id = ? AND enabled = 1", (user_id,)).fetchall()
        conn.close()
        
        if not channels:
            await update.message.reply_text("❌ У вас немає налаштованих каналів")
            return
        
        enabled_ids = {ch[0] for ch in notifications}
        msg = "🔔 Сповіщення:\n\n"
        for (channel_id,) in channels:
            status = "✅ увімкнено" if channel_id in enabled_ids else "❌ вимкнено"
            msg += f"{channel_id}: {status}\n"
        
        msg += "\nВикористання:\n/notify <channel_id> on - увімкнути\n/notify <channel_id> off - вимкнути"
        await update.message.reply_text(msg)
        return
    
    if len(context.args) < 2:
        await update.message.reply_text("Використання: /notify <channel_id|@username> <on|off>")
        return
    
    channel_id = await resolve_channel_id(context, context.args[0])
    if channel_id is None:
        await update.message.reply_text("❌ Невірний ID або username каналу")
        return
    
    action = context.args[1].lower()
    if action not in ['on', 'off']:
        await update.message.reply_text("❌ Використовуйте 'on' або 'off'")
        return
    
    if not is_owner(channel_id, user_id):
        await update.message.reply_text("❌ Ви не є власником цього каналу")
        return
    
    config = get_channel_config(channel_id)
    if config["owner_id"] is None:
        await update.message.reply_text("❌ Канал не налаштований")
        return
    
    enabled = 1 if action == 'on' else 0
    conn = sqlite3.connect(DB_FILE)
    conn.execute("INSERT OR REPLACE INTO notifications (user_id, channel_id, enabled) VALUES (?, ?, ?)",
                 (user_id, channel_id, enabled))
    conn.commit()
    conn.close()
    
    status_text = "увімкнено" if enabled else "вимкнено"
    await update.message.reply_text(f"✅ Сповіщення {status_text}")

async def pause_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Використання: /pause <channel_id|@username> <on|off>")
        return
    
    if len(context.args) < 2:
        await update.message.reply_text("Використання: /pause <channel_id|@username> <on|off>")
        return
    
    channel_id = await resolve_channel_id(context, context.args[0])
    if channel_id is None:
        await update.message.reply_text("❌ Невірний ID або username каналу")
        return
    
    action = context.args[1].lower()
    if action not in ['on', 'off']:
        await update.message.reply_text("❌ Використовуйте 'on' (призупинити) або 'off' (відновити)")
        return
    
    user_id = update.message.from_user.id
    
    if not is_owner(channel_id, user_id):
        await update.message.reply_text("❌ Ви не є власником цього каналу")
        return
    
    config = get_channel_config(channel_id)
    if config["owner_id"] is None:
        await update.message.reply_text("❌ Канал не налаштований")
        return
    
    paused = 1 if action == 'on' else 0
    conn = sqlite3.connect(DB_FILE)
    conn.execute("UPDATE channels SET paused = ? WHERE channel_id = ?", (paused, channel_id))
    conn.commit()
    conn.close()
    
    if paused:
        await update.message.reply_text("⏸️ Моніторинг призупинено. Бот не буде відстежувати зміни статусу.")
    else:
        await update.message.reply_text("▶️ Моніторинг відновлено.")

async def stop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Alias for /pause on"""
    if not context.args:
        await update.message.reply_text("Використання: /stop <channel_id|@username>")
        return
    
    # Add 'on' argument and call pause_cmd
    context.args.append('on')
    await pause_cmd(update, context)

async def resume_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Alias for /pause off"""
    if not context.args:
        await update.message.reply_text("Використання: /resume <channel_id|@username>")
        return
    
    # Add 'off' argument and call pause_cmd
    context.args.append('off')
    await pause_cmd(update, context)

async def export_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        await update.message.reply_text("Використання: /export <channel_id|@username> <csv|json>")
        return
    
    channel_id = await resolve_channel_id(context, context.args[0])
    if channel_id is None:
        await update.message.reply_text("❌ Невірний ID або username каналу")
        return
    
    format_type = context.args[1].lower()
    if format_type not in ['csv', 'json']:
        await update.message.reply_text("❌ Формат має бути 'csv' або 'json'")
        return
    
    user_id = update.message.from_user.id
    
    if not is_owner(channel_id, user_id):
        await update.message.reply_text("❌ Ви не є власником цього каналу")
        return
    
    config = get_channel_config(channel_id)
    if config["owner_id"] is None:
        await update.message.reply_text("❌ Канал не налаштований")
        return
    
    # Get all history
    conn = sqlite3.connect(DB_FILE)
    rows = conn.execute(
        "SELECT status, timestamp FROM history WHERE channel_id = ? ORDER BY timestamp ASC",
        (channel_id,)
    ).fetchall()
    conn.close()
    
    if not rows:
        await update.message.reply_text("📜 Історія порожня")
        return
    
    tz = pytz.timezone(config["timezone"])
    
    if format_type == 'csv':
        import io
        output = io.StringIO()
        output.write("timestamp,status,datetime,duration_minutes\n")
        
        prev_timestamp = None
        for status, timestamp in rows:
            dt = datetime.fromtimestamp(timestamp, tz)
            status_text = "on" if status == 1 else "off"
            duration = int((timestamp - prev_timestamp) / 60) if prev_timestamp else 0
            output.write(f"{int(timestamp)},{status_text},{dt.strftime('%Y-%m-%d %H:%M:%S')},{duration}\n")
            prev_timestamp = timestamp
        
        # Add current period
        now = datetime.now(tz).timestamp()
        duration = int((now - prev_timestamp) / 60)
        current_status = "on" if config["is_power_on"] else "off"
        dt_now = datetime.fromtimestamp(now, tz)
        output.write(f"{int(now)},{current_status},{dt_now.strftime('%Y-%m-%d %H:%M:%S')},{duration}\n")
        
        filename = f"channel_{channel_id}_export.csv"
        await update.message.reply_document(
            document=output.getvalue().encode('utf-8'),
            filename=filename,
            caption=f"📊 Експорт даних ({len(rows)+1} записів)"
        )
    else:  # json
        import json
        data = {
            "channel_id": channel_id,
            "timezone": config["timezone"],
            "export_date": datetime.now(tz).isoformat(),
            "total_events": len(rows) + 1,
            "history": []
        }
        
        prev_timestamp = None
        for status, timestamp in rows:
            dt = datetime.fromtimestamp(timestamp, tz)
            status_text = "on" if status == 1 else "off"
            duration = int((timestamp - prev_timestamp) / 60) if prev_timestamp else 0
            data["history"].append({
                "timestamp": int(timestamp),
                "status": status_text,
                "datetime": dt.strftime('%Y-%m-%d %H:%M:%S'),
                "duration_minutes": duration
            })
            prev_timestamp = timestamp
        
        # Add current period
        now = datetime.now(tz).timestamp()
        duration = int((now - prev_timestamp) / 60)
        current_status = "on" if config["is_power_on"] else "off"
        dt_now = datetime.fromtimestamp(now, tz)
        data["history"].append({
            "timestamp": int(now),
            "status": current_status,
            "datetime": dt_now.strftime('%Y-%m-%d %H:%M:%S'),
            "duration_minutes": duration
        })
        
        filename = f"channel_{channel_id}_export.json"
        await update.message.reply_document(
            document=json.dumps(data, indent=2, ensure_ascii=False).encode('utf-8'),
            filename=filename,
            caption=f"📊 Експорт даних ({len(rows)+1} записів)"
        )

async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    
    if not context.args:
        # Show all channels
        conn = sqlite3.connect(DB_FILE)
        channels = conn.execute("SELECT channel_id, timezone FROM channels WHERE owner_id = ?", (user_id,)).fetchall()
        conn.close()
        
        if not channels:
            await update.message.reply_text("❌ У вас немає налаштованих каналів")
            return
        
        online = []
        offline = []
        no_data = []
        
        for channel_id, timezone in channels:
            config = get_channel_config(channel_id)
            if config["last_request_time"] is None:
                no_data.append((channel_id, timezone))
            else:
                tz = pytz.timezone(timezone)
                now = datetime.now(tz).timestamp()
                time_since = now - config["last_request_time"]
                if config["is_power_on"]:
                    online.append((channel_id, timezone, time_since))
                else:
                    offline.append((channel_id, timezone, time_since))
        
        msg = f"📊 Ваші канали ({len(channels)} всього)\n\n"
        
        if online:
            msg += f"🟢 Онлайн ({len(online)}):\n"
            for channel_id, tz, time_since in online:
                msg += f"  {channel_id} ({tz})\n  └ {format_duration(time_since)} тому\n"
            msg += "\n"
        
        if offline:
            msg += f"🔴 Офлайн ({len(offline)}):\n"
            for channel_id, tz, time_since in offline:
                msg += f"  {channel_id} ({tz})\n  └ {format_duration(time_since)} тому\n"
            msg += "\n"
        
        if no_data:
            msg += f"⚠️ Немає даних ({len(no_data)}):\n"
            for channel_id, tz in no_data:
                msg += f"  {channel_id} ({tz})\n"
        
        await update.message.reply_text(msg)
        return
    
    channel_id = await resolve_channel_id(context, context.args[0])
    if channel_id is None:
        await update.message.reply_text("❌ Невірний ID або username каналу")
        return
    
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
        
        # Add daily stats
        stats = get_daily_stats(channel["channel_id"], channel["timezone"])
        if stats:
            uptime_str = format_duration(stats["uptime"])
            downtime_str = format_duration(stats["downtime"])
            message += f"\n\n📊 Сьогодні: {uptime_str} онлайн, {downtime_str} офлайн ({stats['outages']} відключень)"
        
        if telegram_app:
            # Send to channel
            await telegram_app.bot.send_message(
                chat_id=channel["channel_id"],
                text=message
            )
            
            # Send DM notifications to users who enabled them
            conn = sqlite3.connect(DB_FILE)
            users = conn.execute(
                "SELECT user_id FROM notifications WHERE channel_id = ? AND enabled = 1",
                (channel["channel_id"],)
            ).fetchall()
            conn.close()
            
            for (user_id,) in users:
                try:
                    await telegram_app.bot.send_message(
                        chat_id=user_id,
                        text=f"🔔 Канал {channel['channel_id']}\n\n{message}"
                    )
                except Exception:
                    pass  # User might have blocked the bot
    
    return web.Response(text="OK")

async def check_timeouts():
    """Background task to check for timeouts"""
    while True:
        await asyncio.sleep(30)  # Check every 30 seconds
        
        conn = sqlite3.connect(DB_FILE)
        cur = conn.execute("SELECT channel_id, api_key, timezone, last_request_time, is_power_on, last_status_change FROM channels WHERE is_power_on = 1 AND paused = 0")
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
                
                # Add daily stats
                stats = get_daily_stats(channel_id, tz_str)
                if stats:
                    uptime_str = format_duration(stats["uptime"])
                    downtime_str = format_duration(stats["downtime"])
                    message += f"\n\n📊 Сьогодні: {uptime_str} онлайн, {downtime_str} офлайн ({stats['outages']} відключень)"
                
                if telegram_app:
                    try:
                        # Send to channel
                        await telegram_app.bot.send_message(
                            chat_id=channel_id,
                            text=message
                        )
                        
                        # Send DM notifications
                        conn_notify = sqlite3.connect(DB_FILE)
                        users = conn_notify.execute(
                            "SELECT user_id FROM notifications WHERE channel_id = ? AND enabled = 1",
                            (channel_id,)
                        ).fetchall()
                        conn_notify.close()
                        
                        for (user_id,) in users:
                            try:
                                await telegram_app.bot.send_message(
                                    chat_id=user_id,
                                    text=f"🔔 Канал {channel_id}\n\n{message}"
                                )
                            except Exception:
                                pass  # User might have blocked the bot
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
    telegram_app.add_handler(CommandHandler("regenerate_key", regenerate_key_cmd))
    telegram_app.add_handler(CommandHandler("replace_key", replace_key_cmd))
    telegram_app.add_handler(CommandHandler("remove_channel", remove_channel_cmd))
    telegram_app.add_handler(CommandHandler("transfer", transfer_cmd))
    telegram_app.add_handler(CommandHandler("history", history_cmd))
    telegram_app.add_handler(CommandHandler("notify", notify_cmd))
    telegram_app.add_handler(CommandHandler("pause", pause_cmd))
    telegram_app.add_handler(CommandHandler("stop", stop_cmd))
    telegram_app.add_handler(CommandHandler("resume", resume_cmd))
    telegram_app.add_handler(CommandHandler("export", export_cmd))
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
