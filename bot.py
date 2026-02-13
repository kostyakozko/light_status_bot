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
DB_FILE = "/var/lib/light_status/config.db"
os.makedirs(os.path.dirname(DB_FILE), exist_ok=True)

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
            paused INTEGER DEFAULT 0,
            channel_name TEXT
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
    conn.execute("""
        CREATE TABLE IF NOT EXISTS whitelist (
            channel_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            added_by INTEGER NOT NULL,
            added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (channel_id, user_id),
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

def update_channel_name(channel_id, channel_name):
    """Update channel name in database"""
    conn = sqlite3.connect(DB_FILE)
    conn.execute("UPDATE channels SET channel_name = ? WHERE channel_id = ?", (channel_name, channel_id))
    conn.commit()
    conn.close()

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
    
    # Get status at midnight (last event before today, or current status)
    status_at_midnight = conn.execute(
        "SELECT status FROM history WHERE channel_id = ? AND timestamp < ? ORDER BY timestamp DESC LIMIT 1",
        (channel_id, today_start)
    ).fetchone()
    
    # If no events today and no history before today, get current status
    if not rows and not status_at_midnight:
        cur = conn.execute("SELECT is_power_on, last_status_change FROM channels WHERE channel_id = ?", (channel_id,))
        channel = cur.fetchone()
        conn.close()
        
        if not channel or channel[1] is None:
            return None
        
        # Calculate time from midnight to now in current status
        duration = now_ts - today_start
        if channel[0] == 1:  # is_power_on
            return {"uptime": duration, "downtime": 0, "outages": 0}
        else:
            return {"uptime": 0, "downtime": duration, "outages": 0}
    
    conn.close()
    
    uptime = 0
    downtime = 0
    outages = 0
    
    # Start from midnight with status at that time
    if status_at_midnight:
        prev_status = status_at_midnight[0]
        # If day started with power OFF, count it as 1 outage
        if prev_status == 0:
            outages = 1
    elif rows:
        # No history before today, use first event's status
        prev_status = rows[0][0]
    else:
        return None
    
    prev_time = today_start  # Start counting from midnight
    
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
    user = update.message.from_user
    print(f"User {user.id} (@{user.username if user.username else 'no username'}) sent /start")
    
    await update.message.reply_text(
        "Команди:\n"
        "/create_channel <channel_id|@username> - створити новий канал\n"
        "/import_channel <channel_id|@username> <key> - імпортувати з ключем\n"
        "/get_key <channel_id|@username> - отримати API ключ\n"
        "/list_keys - показати всі канали та ключі\n"
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
        "/status - показати всі канали\n"
        "/whitelist_add <channel_id|@username> <user_id> - додати до whitelist\n"
        "/whitelist_remove <channel_id|@username> <user_id> - видалити з whitelist\n"
        "/whitelist_list <channel_id|@username> - показати whitelist\n\n"
        f"👤 Ваш Telegram ID: `{user.id}`\n\n"
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

async def list_keys_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show all channels and their keys for the user"""
    user_id = update.message.from_user.id
    
    conn = sqlite3.connect(DB_FILE)
    channels = conn.execute(
        "SELECT channel_id, api_key FROM channels WHERE owner_id = ? ORDER BY channel_id",
        (user_id,)
    ).fetchall()
    conn.close()
    
    if not channels:
        await update.message.reply_text("❌ У вас немає налаштованих каналів")
        return
    
    msg = f"🔑 Ваші канали та ключі ({len(channels)}):\n\n"
    for channel_id, api_key in channels:
        # Try to get channel name
        try:
            chat = await context.bot.get_chat(channel_id)
            if chat.username:
                channel_name = f"@{chat.username}"
            elif chat.title:
                channel_name = chat.title
            else:
                channel_name = str(channel_id)
            msg += f"📺 Канал: {channel_name} (`{channel_id}`)\n"
        except Exception:
            msg += f"📺 Канал: `{channel_id}`\n"
        
        msg += f"🔑 Ключ: `{api_key}`\n\n"
    
    msg += f"Використання:\n`curl http://YOUR_SERVER:{HTTP_PORT}/channelPing?channel_key=YOUR_KEY`"
    
    await update.message.reply_text(msg)

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
    user_id = update.message.from_user.id
    
    if not context.args:
        # Show history for all user's channels
        conn = sqlite3.connect(DB_FILE)
        channels = conn.execute(
            "SELECT channel_id, channel_name, timezone FROM channels WHERE owner_id = ?",
            (user_id,)
        ).fetchall()
        
        if not channels:
            conn.close()
            await update.message.reply_text("❌ У вас немає налаштованих каналів")
            return
        
        msg = "📜 Історія всіх каналів (останні події):\n\n"
        
        for channel_id, channel_name, timezone in channels:
            tz = pytz.timezone(timezone)
            
            # Get channel display name
            try:
                chat = await context.bot.get_chat(channel_id)
                if chat.username:
                    display_name = f"@{chat.username}"
                elif chat.title:
                    display_name = chat.title
                else:
                    display_name = str(channel_id)
            except Exception:
                display_name = channel_name or str(channel_id)
            
            rows = conn.execute(
                "SELECT status, timestamp FROM history WHERE channel_id = ? ORDER BY timestamp DESC LIMIT 10",
                (channel_id,)
            ).fetchall()
            
            if rows:
                msg += f"📍 {display_name}:\n"
                for status, timestamp in rows:
                    dt = datetime.fromtimestamp(timestamp, tz)
                    status_emoji = "🟢" if status == 1 else "🔴"
                    status_text = "з'явилося" if status == 1 else "зникло"
                    msg += f"  {status_emoji} {dt.strftime('%d.%m %H:%M')} {status_text}\n"
                msg += "\n"
        
        conn.close()
        await update.message.reply_text(msg)
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
            # Get channel name
            try:
                chat = await context.bot.get_chat(channel_id)
                if chat.username:
                    channel_name = f"@{chat.username}"
                elif chat.title:
                    channel_name = chat.title
                else:
                    channel_name = str(channel_id)
            except Exception:
                channel_name = str(channel_id)
            
            config = get_channel_config(channel_id)
            if config["last_request_time"] is None:
                no_data.append((channel_name, channel_id, timezone))
            else:
                tz = pytz.timezone(timezone)
                now = datetime.now(tz).timestamp()
                time_since = now - config["last_request_time"]
                if config["is_power_on"]:
                    online.append((channel_name, channel_id, timezone, time_since))
                else:
                    offline.append((channel_name, channel_id, timezone, time_since))
        
        msg = f"📊 Ваші канали ({len(channels)} всього)\n\n"
        
        if online:
            msg += f"🟢 Онлайн ({len(online)}):\n"
            for channel_name, channel_id, tz, time_since in online:
                msg += f"  {channel_name} (`{channel_id}`)\n"
                msg += f"  └ {format_duration(time_since)} тому • {tz}\n"
            msg += "\n"
        
        if offline:
            msg += f"🔴 Офлайн ({len(offline)}):\n"
            for channel_name, channel_id, tz, time_since in offline:
                msg += f"  {channel_name} (`{channel_id}`)\n"
                msg += f"  └ {format_duration(time_since)} тому • {tz}\n"
            msg += "\n"
        
        if no_data:
            msg += f"⚠️ Немає даних ({len(no_data)}):\n"
            for channel_name, channel_id, tz in no_data:
                msg += f"  {channel_name} (`{channel_id}`) • {tz}\n"
        
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

async def whitelist_add_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        await update.message.reply_text("Використання: /whitelist_add <channel_id|@username> <user_id>")
        return
    
    user_id = update.message.from_user.id
    channel_id = await resolve_channel_id(context, context.args[0])
    
    if channel_id is None:
        await update.message.reply_text("❌ Невірний ID або username каналу")
        return
    
    if not is_owner(channel_id, user_id):
        await update.message.reply_text("❌ Тільки власник може керувати whitelist")
        return
    
    try:
        target_user_id = int(context.args[1])
    except ValueError:
        await update.message.reply_text("❌ Невірний user_id")
        return
    
    if target_user_id == user_id:
        await update.message.reply_text("❌ Ви вже є власником каналу")
        return
    
    conn = sqlite3.connect(DB_FILE)
    try:
        conn.execute(
            "INSERT INTO whitelist (channel_id, user_id, added_by) VALUES (?, ?, ?)",
            (channel_id, target_user_id, user_id)
        )
        conn.commit()
        await update.message.reply_text(f"✅ Користувач {target_user_id} додано до whitelist")
    except sqlite3.IntegrityError:
        await update.message.reply_text("❌ Користувач вже в whitelist")
    finally:
        conn.close()

async def whitelist_remove_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        await update.message.reply_text("Використання: /whitelist_remove <channel_id|@username> <user_id>")
        return
    
    user_id = update.message.from_user.id
    channel_id = await resolve_channel_id(context, context.args[0])
    
    if channel_id is None:
        await update.message.reply_text("❌ Невірний ID або username каналу")
        return
    
    if not is_owner(channel_id, user_id):
        await update.message.reply_text("❌ Тільки власник може керувати whitelist")
        return
    
    try:
        target_user_id = int(context.args[1])
    except ValueError:
        await update.message.reply_text("❌ Невірний user_id")
        return
    
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.execute(
        "DELETE FROM whitelist WHERE channel_id = ? AND user_id = ?",
        (channel_id, target_user_id)
    )
    conn.commit()
    
    if cursor.rowcount > 0:
        await update.message.reply_text(f"✅ Користувач {target_user_id} видалено з whitelist")
    else:
        await update.message.reply_text("❌ Користувач не знайдено в whitelist")
    conn.close()

async def whitelist_list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Використання: /whitelist_list <channel_id|@username>")
        return
    
    user_id = update.message.from_user.id
    channel_id = await resolve_channel_id(context, context.args[0])
    
    if channel_id is None:
        await update.message.reply_text("❌ Невірний ID або username каналу")
        return
    
    if not is_owner(channel_id, user_id):
        await update.message.reply_text("❌ Тільки власник може переглядати whitelist")
        return
    
    conn = sqlite3.connect(DB_FILE)
    rows = conn.execute(
        "SELECT user_id, added_at FROM whitelist WHERE channel_id = ? ORDER BY added_at",
        (channel_id,)
    ).fetchall()
    conn.close()
    
    if not rows:
        await update.message.reply_text("📋 Whitelist порожній")
        return
    
    msg = f"📋 Whitelist для каналу {channel_id}:\n\n"
    for target_user_id, added_at in rows:
        msg += f"• {target_user_id} (додано {added_at})\n"
    
    await update.message.reply_text(msg)

async def handle_forwarded(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg:
        return
    
    # Debug: log what we receive
    print(f"Forwarded message received. Has forward_origin: {hasattr(msg, 'forward_origin')}")
    if hasattr(msg, 'forward_origin'):
        print(f"Forward origin type: {type(msg.forward_origin)}")
        print(f"Forward origin: {msg.forward_origin}")
    
    if hasattr(msg, 'forward_origin') and msg.forward_origin:
        origin = msg.forward_origin
        
        # Check for SenderUser type (forwarded from user)
        if hasattr(origin, 'sender_user') and origin.sender_user:
            user = origin.sender_user
            response = f"👤 User ID: `{user.id}`"
            if user.username:
                response += f"\nUsername: @{user.username}"
            await msg.reply_text(response)
            return
        
        # Check for channel
        if hasattr(origin, 'chat') and origin.chat:
            if origin.chat.type == "channel":
                channel_id = origin.chat.id
                await msg.reply_text(
                    f"ID каналу: {channel_id}\n\n"
                    f"Використайте: /create_channel {channel_id}"
                )
            elif origin.chat.type == "private":
                user_id = origin.chat.id
                username = origin.chat.username if hasattr(origin.chat, 'username') else None
                response = f"👤 User ID: `{user_id}`"
                if username:
                    response += f"\nUsername: @{username}"
                await msg.reply_text(response)

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
                message = f"🔴 {time_str} Електрохарчування відсутнє\n🕓 Статус невідомий (бот щойно доданий)"
            else:
                now_ts = now.timestamp()
                time_since = now_ts - config["last_request_time"]
                timeout_seconds = TIMEOUT_MINUTES * 60
                
                if time_since > timeout_seconds:
                    # Offline
                    message = f"🔴 {time_str} Електрохарчування відсутнє\n🕓 Останній запит: {format_duration(time_since)} тому"
                else:
                    # Online
                    message = f"🟢 {time_str} Електрохарчування є\n🕓 Останній запит: {format_duration(time_since)} тому"
            
            try:
                await context.bot.send_message(chat_id=channel_id, text=message)
            except Exception as e:
                print(f"Error sending initial status to {channel_id}: {e}")

# HTTP server for ping requests
telegram_app = None

async def handle_dashboard(request):
    """Simple public dashboard for a channel"""
    channel_input = request.match_info.get('channel_id') or request.match_info.get('username')
    
    if not channel_input:
        return web.Response(text="Missing channel_id or username", status=400)
    
    # Try to resolve username or parse ID
    if channel_input.startswith('@') or not channel_input.lstrip('-').isdigit():
        # It's a username, resolve it
        try:
            if telegram_app:
                chat = await telegram_app.bot.get_chat(channel_input if channel_input.startswith('@') else f"@{channel_input}")
                channel_id = chat.id
            else:
                return web.Response(text="Bot not ready", status=503)
        except Exception:
            return web.Response(text="Channel not found", status=404)
    else:
        # It's a numeric ID
        try:
            channel_id = int(channel_input)
        except ValueError:
            return web.Response(text="Invalid channel_id", status=400)
    
    config = get_channel_config(channel_id)
    if config["owner_id"] is None:
        return web.Response(text="Channel not found", status=404)
    
    # Get current status
    tz = pytz.timezone(config["timezone"])
    now = datetime.now(tz)
    
    if config["last_request_time"]:
        last_ping = datetime.fromtimestamp(config["last_request_time"], tz)
        time_since_ping = now.timestamp() - config["last_request_time"]
        last_ping_str = last_ping.strftime("%Y-%m-%d %H:%M:%S")
        time_since_str = format_duration(time_since_ping)
    else:
        last_ping_str = "Never"
        time_since_str = "N/A"
    
    status = "🟢 Online" if config["is_power_on"] else "🔴 Offline"
    status_color = "#4CAF50" if config["is_power_on"] else "#f44336"
    
    # Get daily stats
    stats = get_daily_stats(channel_id, config["timezone"])
    if stats:
        uptime_str = format_duration(stats["uptime"])
        downtime_str = format_duration(stats["downtime"])
        outages = stats["outages"]
    else:
        uptime_str = "N/A"
        downtime_str = "N/A"
        outages = 0
    
    # Get channel name
    try:
        if telegram_app:
            chat = await telegram_app.bot.get_chat(channel_id)
            if chat.username:
                channel_name = f"@{chat.username}"
            elif chat.title:
                channel_name = chat.title
            else:
                channel_name = f"Channel {channel_id}"
            # Save to database for Grafana
            update_channel_name(channel_id, channel_name)
        else:
            channel_name = f"Channel {channel_id}"
    except Exception:
        channel_name = f"Channel {channel_id}"
    
    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>{channel_name} - Power Status</title>
        <style>
            body {{
                font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, Cantarell, sans-serif;
                max-width: 800px;
                margin: 0 auto;
                padding: 20px;
                background: #f5f5f5;
            }}
            .card {{
                background: white;
                border-radius: 8px;
                padding: 20px;
                margin-bottom: 20px;
                box-shadow: 0 2px 4px rgba(0,0,0,0.1);
            }}
            h1 {{
                margin: 0 0 10px 0;
                color: #333;
            }}
            .status {{
                font-size: 48px;
                font-weight: bold;
                color: {status_color};
                margin: 20px 0;
            }}
            .info {{
                display: grid;
                grid-template-columns: 1fr 1fr;
                gap: 15px;
                margin-top: 20px;
            }}
            .info-item {{
                padding: 15px;
                background: #f9f9f9;
                border-radius: 4px;
            }}
            .info-label {{
                font-size: 12px;
                color: #666;
                text-transform: uppercase;
                margin-bottom: 5px;
            }}
            .info-value {{
                font-size: 20px;
                font-weight: bold;
                color: #333;
            }}
            .footer {{
                text-align: center;
                color: #999;
                font-size: 12px;
                margin-top: 20px;
            }}
            @media (max-width: 600px) {{
                .info {{
                    grid-template-columns: 1fr;
                }}
            }}
        </style>
        <meta http-equiv="refresh" content="30">
    </head>
    <body>
        <div class="card">
            <h1>{channel_name}</h1>
            <div class="status">{status}</div>
            <div style="color: #666;">Last ping: {last_ping_str} ({time_since_str} ago)</div>
        </div>
        
        <div class="card">
            <h2 style="margin-top: 0;">Today's Statistics</h2>
            <div class="info">
                <div class="info-item">
                    <div class="info-label">Uptime</div>
                    <div class="info-value" style="color: #4CAF50;">{uptime_str}</div>
                </div>
                <div class="info-item">
                    <div class="info-label">Downtime</div>
                    <div class="info-value" style="color: #f44336;">{downtime_str}</div>
                </div>
                <div class="info-item">
                    <div class="info-label">Outages</div>
                    <div class="info-value">{outages}</div>
                </div>
                <div class="info-item">
                    <div class="info-label">Timezone</div>
                    <div class="info-value" style="font-size: 16px;">{config["timezone"]}</div>
                </div>
            </div>
        </div>
        
        <div class="footer">
            Auto-refreshes every 30 seconds • Powered by Light Status Bot
        </div>
    </body>
    </html>
    """
    
    return web.Response(text=html, content_type='text/html')

async def handle_api_channels(request):
    """API endpoint for Grafana - list all channels"""
    conn = sqlite3.connect(DB_FILE)
    channels = conn.execute(
        "SELECT channel_id, channel_name, is_power_on, last_request_time FROM channels WHERE owner_id IS NOT NULL"
    ).fetchall()
    conn.close()
    
    result = []
    for ch_id, ch_name, is_on, last_req in channels:
        result.append({
            "channel_id": ch_id,
            "channel_name": ch_name or f"Channel {ch_id}",
            "status": "online" if is_on else "offline",
            "last_ping": last_req
        })
    
    return web.json_response(result)

async def handle_api_history(request):
    """API endpoint for Grafana - status history"""
    conn = sqlite3.connect(DB_FILE)
    history = conn.execute("""
        SELECT h.timestamp, h.channel_id, c.channel_name, h.status 
        FROM history h 
        LEFT JOIN channels c ON h.channel_id = c.channel_id 
        ORDER BY h.timestamp DESC 
        LIMIT 1000
    """).fetchall()
    conn.close()
    
    result = []
    for ts, ch_id, ch_name, status in history:
        result.append({
            "timestamp": int(ts * 1000),  # milliseconds for Grafana
            "channel_id": ch_id,
            "channel_name": ch_name or f"Channel {ch_id}",
            "status": status
        })
    
    return web.json_response(result)

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
        
        message = f"🟢 {time_str} Електрохарчування відновлено\n🕓 Його не було {duration_text}"
        
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
    global telegram_app
    print("Timeout checker started")
    while True:
        await asyncio.sleep(30)  # Check every 30 seconds
        print(f"Checking timeouts... telegram_app is {'set' if telegram_app else 'None'}")
        
        conn = sqlite3.connect(DB_FILE)
        cur = conn.execute("SELECT channel_id, api_key, timezone, last_request_time, is_power_on, last_status_change FROM channels WHERE is_power_on = 1 AND paused = 0")
        channels = cur.fetchall()
        conn.close()
        
        now = datetime.now().timestamp()
        timeout_seconds = TIMEOUT_MINUTES * 60
        
        for row in channels:
            channel_id, api_key, tz_str, last_req, is_on, last_change = row
            
            if last_req and (now - last_req) > timeout_seconds:
                # Power is off - use last_req as the OFF time, not now
                update_power_status(api_key, False, last_req)
                
                # Calculate how long it was on
                if last_change:
                    duration = last_req - last_change
                    duration_text = format_duration(duration)
                else:
                    duration_text = "невідомо"
                
                # Send Telegram message
                tz = pytz.timezone(tz_str)
                time_str = datetime.fromtimestamp(last_req, tz).strftime("%H:%M")
                
                message = f"🔴 {time_str} Електрохарчування відсутнє\n🕓 Воно було {duration_text}"
                
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
    global telegram_app
    telegram_app = Application.builder().token(token).build()
    
    telegram_app.add_handler(CommandHandler("start", start))
    telegram_app.add_handler(CommandHandler("create_channel", create_channel_cmd))
    telegram_app.add_handler(CommandHandler("import_channel", import_channel_cmd))
    telegram_app.add_handler(CommandHandler("get_key", get_key_cmd))
    telegram_app.add_handler(CommandHandler("list_keys", list_keys_cmd))
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
    telegram_app.add_handler(CommandHandler("whitelist_add", whitelist_add_cmd))
    telegram_app.add_handler(CommandHandler("whitelist_remove", whitelist_remove_cmd))
    telegram_app.add_handler(CommandHandler("whitelist_list", whitelist_list_cmd))
    telegram_app.add_handler(MessageHandler(filters.FORWARDED & filters.ChatType.PRIVATE, handle_forwarded))
    telegram_app.add_handler(ChatMemberHandler(handle_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))
    
    # Start HTTP server
    app = web.Application()
    app.router.add_get('/api/channels', handle_api_channels)
    app.router.add_get('/api/history', handle_api_history)
    app.router.add_get('/status/@{username}', handle_dashboard)
    app.router.add_get('/status/{channel_id}', handle_dashboard)
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
