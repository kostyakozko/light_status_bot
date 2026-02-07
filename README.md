# Light Status Bot

Telegram bot for monitoring power/light status via HTTP requests.

## Features

- Monitor multiple channels from one Telegram account
- HTTP endpoint for status updates (`/channelPing?channel_key=KEY`)
- Automatic status detection (5 min timeout)
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
curl http://YOUR_SERVER:8080/channelPing?channel_key=YOUR_KEY
```

### Option 2: Import Existing Channel (From Original Bot)
1. Forward message from your channel to bot
2. Use `/import_channel <channel_id> <existing_key>`
3. Add to your existing ping script (alongside original bot):
```bash
curl http://api.svitlobot.in.ua/channelPing?channel_key=AWAHFETGAL
curl http://YOUR_SERVER:8080/channelPing?channel_key=AWAHFETGAL
```

Both bots will receive updates for redundancy!

### Commands
- `/start` - show available commands
- `/create_channel <id>` - create new channel (generates key)
- `/import_channel <id> <key>` - import with existing key
- `/set_channel <id>` - select active channel
- `/get_key` - get API key
- `/set_timezone <tz>` - set timezone (e.g., Europe/Kiev)
- `/status` - check current status

### Timezone Setup
```
/set_timezone Europe/Kiev
/set_timezone Europe/Warsaw
/set_timezone America/New_York
```

Full list: https://en.wikipedia.org/wiki/List_of_tz_database_time_zones

## How it works

- Device sends HTTP requests while power is ON (every 1-2 minutes)
- If no request for 5 minutes → bot posts "🔴 HH:MM Світло зникло"
- When requests resume → bot posts "🟢 HH:MM Світло з'явилося"
- Messages include time and duration in Ukrainian

## Deployment

Bot runs on Oracle Cloud (same server as image bot).
See deployment instructions in image bot's ORACLE_COMMANDS.md.

## Future Improvements

See [GitHub Issues](https://github.com/kostyakozko/light_status_bot/issues) for planned features:
- API key regeneration
- Ownership transfer
- Channel removal
- Daily statistics
- Web dashboard
