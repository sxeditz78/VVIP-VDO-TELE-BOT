# 🤖 Advanced Media Bot (SynaX Edition)

Ek powerful Telegram Media Management bot jo user subscription, auto-approval aur media distribution ko handle karta hai. Ye bot PostgreSQL database ka use karke users ki history aur expiry manage karta hai.

## ✨ Features

* **Premium Access (28 Days):** Users ko 28 din ka trial/subscription milta hai.
* **Auto-Expiry & Ban:** Subscription khatam hote hi user automatically ban ho jata hai.
* **Media History:** Bot yaad rakhta hai user ne kya dekh liya hai aur `/next` dabane par nayi media dikhata hai.
* **Admin Dashboard:** `/stats`, `/broadcast`, `/ban`, `/unban`, aur `/approve` jaise powerful commands.
* **Auto-Delete:** Privacy ke liye media messages 10 minute baad automatically delete ho jate hain.
* **Live Tracking:** Dekhein kitne users abhi active hain.

## 🚀 Deployment (Railway / VPS)

### 1. Requirements
* Python 3.9+
* PostgreSQL Database
* Telegram Bot Token (from [@BotFather](https://t.me/BotFather))

### 2. Environment Variables
Railway ya VPS par niche diye gaye variables set karein:

| Variable | Description |
|----------|-------------|
| `BOT_TOKEN` | Aapka Telegram Bot Token |
| `DATABASE_URL` | PostgreSQL connection string |
| `SOURCE_CHAT_ID` | Jahan se media copy hona hai (Channel ID) |
| `ADMIN_ID` | Aapki numeric Telegram ID |
| `ADMIN_USERNAME` | Aapka username (Optional) |

### 3. Installation
```bash
# Repository clone karein
git clone [https://github.com/your-username/your-repo-name.git](https://github.com/your-username/your-repo-name.git)
cd your-repo-name

# Dependencies install karein
pip install -r requirements.json
