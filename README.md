# Light Status Bot

Telegram bot for monitoring power/light status via HTTP requests.

## Features

- Monitor multiple channels from one Telegram account
- HTTP endpoint for status updates (`/channelPing?channel_key=KEY`)
- API endpoints for external dashboards (`/api/channels`, `/api/history`)
- Simple built-in status page (`/status/{channel_id}`)
- Automatic status detection (5 min timeout)
- Daily statistics with uptime/downtime tracking
- Personal DM notifications
- Pause/resume monitoring
- History export (CSV/JSON)
- Configurable timezone per channel
- Channel ownership management
- Compatible with original svitlobot API keys

## Setup

1. Install dependencies:
```bash
pip install -r requirements.txt
```

2. Create `token.txt` with your bot token from @BotFather

3. Run:
```bash
python bot.py
```

## Configuration

### Option 1: Create New Channel (Standalone)
1. Forward message from your channel to bot
2. Use `/create_channel <channel_id>`
3. Bot generates unique API key
4. Add to your ping script:
```bash
# HTTPS (encrypted, recommended)
curl https://YOUR_DOMAIN/channelPing?channel_key=YOUR_KEY

# HTTP (works everywhere)
curl http://YOUR_DOMAIN/channelPing?channel_key=YOUR_KEY

# Direct IP (if DNS fails)
curl http://YOUR_SERVER_IP:8080/channelPing?channel_key=YOUR_KEY
```

### Option 2: Import Existing Channel (From Original Bot)
1. Forward message from your channel to bot
2. Use `/import_channel <channel_id> <existing_key>`
3. Add to your existing ping script (alongside original bot):
```bash
curl http://api.svitlobot.in.ua/channelPing?channel_key=YOUR_KEY
curl https://YOUR_DOMAIN/channelPing?channel_key=YOUR_KEY
curl http://YOUR_SERVER:8080/channelPing?channel_key=AWAHFETGAL
```

Both bots will receive updates for redundancy!

### Commands

**Channel Management:**
- `/start` - show available commands
- `/create_channel <id>` - create new channel (generates key)
- `/import_channel <id> <key>` - import with existing key
- `/set_channel <id>` - select active channel for configuration
- `/remove_channel [id]` - delete channel configuration
- `/transfer <user_id> [channel_id]` - transfer ownership

**Monitoring:**
- `/status [channel_id]` - check current status
- `/get_key [channel_id]` - get API key
- `/list_keys` - list all your channels and keys
- `/pause [channel_id]` - pause monitoring (no timeout messages)
- `/resume [channel_id]` - resume monitoring
- `/stop [channel_id]` - alias for pause

**Configuration:**
- `/set_timezone <tz> [channel_id]` - set timezone (e.g., Europe/Kiev)
- `/notify [channel_id]` - toggle DM notifications for status changes

**History & Export:**
- `/history [channel_id]` - show recent status changes
- `/export <format> [channel_id]` - export history (csv or json)

**Note:** Most commands accept optional `channel_id` or `@username` parameter. If omitted, uses currently selected channel.

### Timezone Setup
```
/set_timezone Europe/Kiev
/set_timezone Europe/Warsaw
/set_timezone America/New_York
```

Full list: https://en.wikipedia.org/wiki/List_of_tz_database_time_zones

## API Endpoints

The bot provides HTTP endpoints for monitoring and integration:

### Status Updates
```bash
GET /channelPing?channel_key=YOUR_KEY
```
Device endpoint to report "power is on" status.

**Response:** `OK` (200)

### Built-in Status Page
```
GET /status/{channel_id}
GET /status/@channelname
```
Simple HTML page showing current status and today's statistics.

**Example:** `https://YOUR_DOMAIN/status/-1001234567890`

### Data API

#### Get All Channels
```
GET /api/channels
```
Returns list of all configured channels with current status.

**Response:**
```json
[
  {
    "channel_id": -1001234567890,
    "channel_name": "@channelname",
    "is_power_on": true,
    "last_request_time": 1707423456.789,
    "timezone": "Europe/Kiev"
  }
]
```

#### Get Channel History
```
GET /api/history?channel_id={id}&days={n}
```
Returns status change history for specified channel.

**Parameters:**
- `channel_id` (required) - Channel ID
- `days` (optional) - Number of days to retrieve (default: 7)

**Response:**
```json
[
  {
    "timestamp": 1707423456.789,
    "status": 1
  },
  {
    "timestamp": 1707419856.789,
    "status": 0
  }
]
```

**Status values:** `1` = power ON, `0` = power OFF

## How it works

- Device sends HTTP requests while power is ON (every 1-2 minutes)
- If no request for 5 minutes → bot posts "🔴 HH:MM Світло зникло"
- When requests resume → bot posts "🟢 HH:MM Світло з'явилося"
- Messages include time, duration, and daily statistics in Ukrainian
- History is logged to SQLite database for analytics

## Database Schema

```sql
-- Channel configuration
channels: channel_id, owner_id, api_key, timezone, last_request_time, 
          is_power_on, last_status_change, paused, channel_name

-- Status change history
history: id, channel_id, status, timestamp

-- DM notification preferences
notifications: user_id, channel_id, enabled
```

## Deployment

**Service:** `light-status-bot.service` - Telegram bot + HTTP server (port 8080)

**Database:** `/var/lib/light_status/config.db` (SQLite)

**Requirements:**
- Python 3.11+
- Systemd for service management

**Optional:**
- Nginx for reverse proxy and SSL
- Domain name for HTTPS

## Development

**Repository:** https://github.com/kostyakozko/light_status_bot

**Tech stack:**
- Python 3.11+ with python-telegram-bot
- aiohttp for HTTP server
- SQLite for data storage

## License

MIT
