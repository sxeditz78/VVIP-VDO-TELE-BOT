# 🤖 XVIP Telegram Media Bot

A premium Telegram media delivery bot with access control, subscription management, and auto-moderation — powered by **Python**, **python-telegram-bot v21**, and **PostgreSQL**.

---

## ✨ Features

- 🔐 **Admin Approval System** — New users wait for admin approval before accessing content
- 💎 **28-Day Premium Subscription** — Auto-expires access after 28 days with renewal prompt
- 🚫 **Ban / Unban System** — Admin can ban/unban users with reasons
- 📤 **Broadcast Messages** — Send announcements to all active approved users
- 🎬 **Smart Media Delivery** — Fetches unseen media first, with 10% repeat chance
- ⬅️➡️ **Previous / Next Navigation** — Users can navigate through media history
- 🗑️ **Auto-Delete** — Media messages auto-delete after 10 minutes
- 📊 **Live Stats** — Real-time live user count and total joins
- 👁️ **Media Watcher** — Auto-indexes new photos/videos posted in source channel
- ⏰ **Expiry Checker** — Background task runs hourly to auto-ban expired users

---

## 🛠️ Tech Stack

| Component | Technology |
|-----------|-----------|
| Language | Python 3.11+ |
| Bot Framework | python-telegram-bot v21.3 |
| Database | PostgreSQL (Neon recommended) |
| Hosting | Railway |

---

## 🚀 Deploy on Railway

### Step 1 — Fork & Clone

```bash
git clone https://github.com/YOUR_USERNAME/XVIP-TELE-BOT.git
cd XVIP-TELE-BOT
```

### Step 2 — Create Railway Project

1. Go to [railway.app](https://railway.app) and log in
2. Click **New Project** → **Deploy from GitHub repo**
3. Select your forked repo

### Step 3 — Set Environment Variables

In Railway dashboard → your service → **Variables**, add:

| Variable | Description | Example |
|----------|-------------|---------|
| `BOT_TOKEN` | Your Telegram bot token from [@BotFather](https://t.me/BotFather) | `123456:ABC-DEF...` |
| `DATABASE_URL` | PostgreSQL connection string | `postgresql://user:pass@host/db` |
| `SOURCE_CHAT_ID` | Channel/group ID where media is posted | `-1001234567890` |
| `ADMIN_ID` | Your Telegram user ID | `987654321` |
| `ADMIN_USERNAME` | Your Telegram username | `@YourUsername` |

> 💡 **Neon PostgreSQL** is recommended — free tier works great. Get URL from [neon.tech](https://neon.tech)

### Step 4 — Add a Procfile (if not present)

Create a `Procfile` in root:

```
worker: python bot.py
```

> ⚠️ Use `worker` not `web` — this bot uses polling, not a web server.

### Step 5 — Deploy

Railway will auto-deploy on every push to `main`. Watch logs in the Railway dashboard to confirm the bot starts.

---

## ⚙️ Environment Variables Reference

```env
BOT_TOKEN=your_bot_token_here
DATABASE_URL=postgresql://user:password@host:5432/dbname
SOURCE_CHAT_ID=-1001234567890
ADMIN_ID=123456789
ADMIN_USERNAME=@YourUsername
```

---

## 📋 Admin Commands

| Command | Description |
|---------|-------------|
| `/stats` | View live users, total joins, approved, banned, expired, media count |
| `/pending` | List users waiting for approval |
| `/approve <user_id>` | Approve a user (28-day access) |
| `/reject <user_id>` | Reject a user's request |
| `/ban <user_id> [reason]` | Ban a user |
| `/unban <user_id>` | Unban a user |
| `/banned` | List all banned users |
| `/expiring` | Show users expiring in next 3 days |
| `/broadcast <message>` | Send message to all approved users |

---

## 👤 User Flow

```
/start
  │
  ├── New User → Request sent to admin (Approve/Reject buttons)
  │
  ├── Pending → "Request bhej di, wait karo"
  │
  ├── Rejected → "Request reject ho gayi"
  │
  ├── Banned → Renewal prompt + admin notified
  │
  └── Approved → Welcome message + first media delivered
                    ↓
              [⬅️ Previous]  [▶️ Next]
              Auto-deletes after 10 minutes
```

---

## 🗄️ Database Schema

The bot auto-creates all tables on first run:

- `media` — Stores indexed media (message_id, type)
- `users` — User records with approval status and expiry
- `user_history` — Per-user seen media tracking
- `user_position` — Last viewed media position per user
- `banned_users` — Ban list with reasons and timestamps

---

## 📁 Project Structure

```
XVIP-TELE-BOT/
├── bot.py          # Main bot code
├── requirements.txt
├── Procfile        # Railway process config
└── .gitignore
```

---

## 📦 Local Development

```bash
# Install dependencies
pip install -r requirements.txt

# Create .bot.env file
cp .bot.env.example .bot.env
# Fill in your values

# Run
python bot.py
```

---

## 📄 License

Private project. All rights reserved.

---

Made with ❤️ by [@SynaX_69](https://t.me/SynaX_69)
