# Discord Sheet Highlighter Bot

A Discord bot that automatically detects player selections from messages and highlights the corresponding player row in a connected Google Sheet. Designed for the **All Time Draft (ATD)** community.

---

## 🚀 Features

- Reads messages from specific Discord channels.
- Detects player names (direct, surname, or fuzzy match).
- Highlights the player's row in Google Sheets.
- Prevents duplicate highlights.
- Smart reply detection (merges parent + reply content).
- Lightweight logging and auto-reconnect system.

---

## 🧩 Requirements

- Python 3.10+
- Discord bot token
- Google service account (with access to your target spreadsheet)
- `.env` file configured with necessary keys

---

## ⚙️ Setup

### 1. Clone and install dependencies
```bash
git clone https://github.com/yourname/discord-highlighter-bot.git
cd discord-highlighter-bot
pip install -r requirements.txt
