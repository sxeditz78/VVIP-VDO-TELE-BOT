import os
import asyncio
import random
import logging
from datetime import datetime, timedelta

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, Bot,
    BotCommand, BotCommandScopeAllPrivateChats, BotCommandScopeChat
)
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes
)
from telegram.error import TelegramError

import asyncpg

# ─── Config ────────────────────────────────────────────────────────────────────
BOT_TOKEN        = os.environ["BOT_TOKEN"]
DATABASE_URL     = os.environ["DATABASE_URL"]
SOURCE_CHAT_ID   = int(os.environ["SOURCE_CHAT_ID"])
ADMIN_ID         = int(os.environ["ADMIN_ID"])
ADMIN_USERNAME   = os.environ.get("ADMIN_USERNAME", "@SynaX_69")
SUPPORT_USERNAME = os.environ.get("SUPPORT_USERNAME", "@SynaX_69")   # support button username

AUTO_DELETE_SECONDS = 600
REPEAT_CHANCE       = 0.02   # 2% repeat chance
APPROVAL_DAYS       = 28

# Runtime toggle — admin can flip with /support on|off
support_button_enabled: bool = True

BUY_PREMIUM_MSG = (
    "💎 *Aapki Premium Access Expire Ho Gayi!*\n\n"
    "━━━━━━━━━━━━━━━━━━━━\n"
    f"🚫 Aapke *{APPROVAL_DAYS} din* poore ho gaye hain.\n\n"
    "🔓 *Premium Access Kaise Lein?*\n"
    "👉 Admin se sampark karein aur apna subscription renew karein.\n\n"
    f"📩 Admin: {ADMIN_USERNAME}\n"
    "━━━━━━━━━━━━━━━━━━━━\n"
    "✨ _Premium members ko unlimited access milta hai!_"
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Keep strong references to background tasks so GC doesn't destroy them
_background_tasks: set = set()

def _fire_and_forget(coro):
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task

pool: asyncpg.Pool = None

# ─── DB Init ───────────────────────────────────────────────────────────────────
async def init_db():
    global pool
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=10, ssl="prefer")
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS media (
                id          SERIAL PRIMARY KEY,
                message_id  BIGINT UNIQUE NOT NULL,
                media_type  TEXT NOT NULL,
                added_at    TIMESTAMP DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id      BIGINT PRIMARY KEY,
                joined_at    TIMESTAMP DEFAULT NOW(),
                last_seen    TIMESTAMP DEFAULT NOW(),
                is_active    BOOLEAN DEFAULT TRUE,
                is_approved  BOOLEAN DEFAULT FALSE,
                is_rejected  BOOLEAN DEFAULT FALSE,
                approved_at  TIMESTAMP DEFAULT NULL,
                expires_at   TIMESTAMP DEFAULT NULL
            )
        """)
        for col, definition in [
            ("approved_at", "TIMESTAMP DEFAULT NULL"),
            ("expires_at",  "TIMESTAMP DEFAULT NULL"),
        ]:
            try:
                await conn.execute(f"ALTER TABLE users ADD COLUMN IF NOT EXISTS {col} {definition}")
            except Exception:
                pass
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS user_history (
                user_id     BIGINT NOT NULL,
                media_id    INT    NOT NULL,
                seen_at     TIMESTAMP DEFAULT NOW(),
                PRIMARY KEY (user_id, media_id)
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS user_position (
                user_id          BIGINT PRIMARY KEY,
                current_media_id INT,
                bot_message_id   BIGINT,
                updated_at       TIMESTAMP DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS banned_users (
                user_id     BIGINT PRIMARY KEY,
                banned_at   TIMESTAMP DEFAULT NOW(),
                reason      TEXT DEFAULT 'No reason given'
            )
        """)
        # Track 2-day expiry warnings (so we don't spam)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS expiry_notified (
                user_id     BIGINT PRIMARY KEY,
                notified_at TIMESTAMP DEFAULT NOW()
            )
        """)
    logger.info("✅ Database initialized")


# ─── Bot Commands Menu ─────────────────────────────────────────────────────────
async def set_bot_commands(bot: Bot):
    user_commands = [
        BotCommand("start",  "▶️ Bot shuru karo / media dekho"),
        BotCommand("status", "📅 Apni expiry date dekho"),
    ]
    admin_commands = user_commands + [
        BotCommand("stats",     "📊 Bot statistics"),
        BotCommand("pending",   "⏳ Pending approval requests"),
        BotCommand("approve",   "✅ User approve karo"),
        BotCommand("reject",    "❌ User reject karo"),
        BotCommand("ban",       "🚫 User ban karo"),
        BotCommand("unban",     "🔓 User unban karo"),
        BotCommand("banned",    "📋 Banned users list"),
        BotCommand("expiring",  "⚠️ Expiring users (3 din mein)"),
        BotCommand("broadcast", "📢 Sabko message bhejo"),
        BotCommand("support",   "🆘 Support button on/off karo"),
    ]
    await bot.set_my_commands(user_commands, scope=BotCommandScopeAllPrivateChats())
    await bot.set_my_commands(admin_commands, scope=BotCommandScopeChat(chat_id=ADMIN_ID))
    logger.info("✅ Bot commands menu set")


# ─── Expiry Checker (runs hourly) ──────────────────────────────────────────────
async def expiry_checker(bot: Bot):
    while True:
        try:
            # 1) Auto-ban fully expired users
            async with pool.acquire() as conn:
                expired_users = await conn.fetch("""
                    SELECT u.user_id FROM users u
                    WHERE u.is_approved = TRUE
                      AND u.expires_at IS NOT NULL
                      AND u.expires_at < NOW()
                      AND u.user_id NOT IN (SELECT user_id FROM banned_users)
                """)
            for row in expired_users:
                uid = row["user_id"]
                await ban_user(uid, reason=f"{APPROVAL_DAYS}-day premium expired")
                async with pool.acquire() as conn:
                    await conn.execute("UPDATE users SET is_approved = FALSE WHERE user_id = $1", uid)
                try:
                    await bot.send_message(chat_id=uid, text=BUY_PREMIUM_MSG, parse_mode="Markdown")
                except TelegramError:
                    pass
                logger.info(f"⏰ Auto-banned expired user: {uid}")

            # 2) Send 2-day warning (only once per cycle)
            soon = datetime.utcnow() + timedelta(days=2)
            async with pool.acquire() as conn:
                warning_users = await conn.fetch("""
                    SELECT u.user_id, u.expires_at FROM users u
                    WHERE u.is_approved = TRUE
                      AND u.expires_at IS NOT NULL
                      AND u.expires_at BETWEEN NOW() AND $1
                      AND u.user_id NOT IN (SELECT user_id FROM banned_users)
                      AND u.user_id NOT IN (SELECT user_id FROM expiry_notified)
                """, soon)
            for row in warning_users:
                uid        = row["user_id"]
                expires_at = row["expires_at"]
                exp_str    = expires_at.strftime('%d %b %Y')
                msg = (
                    f"⚠️ *Premium Expire Hone Wala Hai!*\n\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"📅 Aapka premium *{exp_str}* ko expire ho jayega.\n\n"
                    f"🔓 Ab hi renew karo — admin se sampark karo:\n"
                    f"📩 {ADMIN_USERNAME}\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"✨ _Access khatam hone se pehle renew karo!_"
                )
                try:
                    await bot.send_message(chat_id=uid, text=msg, parse_mode="Markdown")
                    async with pool.acquire() as conn:
                        await conn.execute("""
                            INSERT INTO expiry_notified (user_id) VALUES ($1)
                            ON CONFLICT (user_id) DO UPDATE SET notified_at = NOW()
                        """, uid)
                    logger.info(f"📬 Expiry warning sent to: {uid}")
                except TelegramError:
                    pass

        except Exception as e:
            logger.error(f"Expiry checker error: {e}")
        await asyncio.sleep(3600)


# ─── Helpers ───────────────────────────────────────────────────────────────────
def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID

async def is_banned(user_id: int) -> bool:
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT user_id FROM banned_users WHERE user_id = $1", user_id)
    return row is not None

async def ban_user(user_id: int, reason: str = "No reason given"):
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO banned_users (user_id, reason)
            VALUES ($1, $2)
            ON CONFLICT (user_id) DO UPDATE SET reason = $2, banned_at = NOW()
        """, user_id, reason)

async def unban_user(user_id: int):
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM banned_users WHERE user_id = $1", user_id)

async def get_all_active_users() -> list[int]:
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT u.user_id FROM users u
            WHERE u.is_active = TRUE AND u.is_approved = TRUE
              AND u.user_id NOT IN (SELECT user_id FROM banned_users)
        """)
    return [r["user_id"] for r in rows]

async def is_approved(user_id: int) -> bool:
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT is_approved FROM users WHERE user_id = $1", user_id)
    return row["is_approved"] if row else False

async def is_rejected(user_id: int) -> bool:
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT is_rejected FROM users WHERE user_id = $1", user_id)
    return row["is_rejected"] if row else False

async def is_expired(user_id: int) -> bool:
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT expires_at FROM users WHERE user_id = $1", user_id)
    if not row or not row["expires_at"]:
        return False
    return datetime.utcnow() > row["expires_at"]

async def approve_user(user_id: int):
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT expires_at FROM users WHERE user_id = $1", user_id)
        existing_expiry = row["expires_at"] if row else None
        if existing_expiry and existing_expiry > datetime.utcnow():
            expires = existing_expiry
        else:
            expires = datetime.utcnow() + timedelta(days=APPROVAL_DAYS)
        await conn.execute("""
            UPDATE users SET is_approved = TRUE, is_rejected = FALSE,
            approved_at = NOW(), expires_at = $2 WHERE user_id = $1
        """, user_id, expires)
        await conn.execute("DELETE FROM banned_users WHERE user_id = $1", user_id)
        await conn.execute("DELETE FROM expiry_notified WHERE user_id = $1", user_id)
    return expires

async def reject_user(user_id: int):
    async with pool.acquire() as conn:
        await conn.execute("UPDATE users SET is_approved = FALSE, is_rejected = TRUE WHERE user_id = $1", user_id)

def approval_keyboard(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(f"✅ Approve ({APPROVAL_DAYS} Days)", callback_data=f"approve_{user_id}"),
        InlineKeyboardButton("❌ Reject",                          callback_data=f"reject_{user_id}"),
    ]])

def ban_request_keyboard(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Approve (Continue Sub)", callback_data=f"approve_{user_id}"),
        InlineKeyboardButton("🚫 Keep Banned",            callback_data=f"keepban_{user_id}"),
    ]])

async def update_last_seen(user_id: int):
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO users (user_id, last_seen, is_active) VALUES ($1, NOW(), TRUE)
            ON CONFLICT (user_id) DO UPDATE SET last_seen = NOW(), is_active = TRUE
        """, user_id)

async def register_user(user_id: int):
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO users (user_id, joined_at, last_seen, is_active) VALUES ($1, NOW(), NOW(), TRUE)
            ON CONFLICT (user_id) DO UPDATE SET last_seen = NOW(), is_active = TRUE
        """, user_id)

async def get_next_media(user_id: int) -> dict | None:
    async with pool.acquire() as conn:
        all_media = await conn.fetch("SELECT id, message_id, media_type FROM media ORDER BY id")
        if not all_media:
            return None
        seen_ids = set(r["media_id"] for r in await conn.fetch("SELECT media_id FROM user_history WHERE user_id = $1", user_id))
        unseen = [m for m in all_media if m["id"] not in seen_ids]
        seen   = [m for m in all_media if m["id"] in seen_ids]
        if unseen and (not seen or random.random() > REPEAT_CHANCE):
            chosen = random.choice(unseen)
        elif seen:
            chosen = random.choice(seen)
        else:
            chosen = random.choice(all_media)
        return dict(chosen)

async def mark_seen(user_id: int, media_id: int):
    async with pool.acquire() as conn:
        await conn.execute("INSERT INTO user_history (user_id, media_id) VALUES ($1, $2) ON CONFLICT DO NOTHING", user_id, media_id)

async def get_position(user_id: int) -> dict | None:
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM user_position WHERE user_id = $1", user_id)
    return dict(row) if row else None

async def save_position(user_id: int, media_id: int, bot_msg_id: int):
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO user_position (user_id, current_media_id, bot_message_id, updated_at)
            VALUES ($1, $2, $3, NOW())
            ON CONFLICT (user_id) DO UPDATE SET current_media_id = $2, bot_message_id = $3, updated_at = NOW()
        """, user_id, media_id, bot_msg_id)

async def get_prev_media(user_id: int, current_media_id: int) -> dict | None:
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT uh.media_id, m.message_id, m.media_type
            FROM user_history uh JOIN media m ON m.id = uh.media_id
            WHERE uh.user_id = $1 AND uh.media_id < $2
            ORDER BY uh.media_id DESC LIMIT 1
        """, user_id, current_media_id)
    return dict(row) if row else None

def media_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton("⬅️ Previous", callback_data="prev"),
            InlineKeyboardButton("▶️ Next",     callback_data="next"),
        ],
    ]
    if support_button_enabled:
        rows.append([
            InlineKeyboardButton("🆘 Support", url=f"https://t.me/{SUPPORT_USERNAME.lstrip('@')}"),
        ])
    return InlineKeyboardMarkup(rows)

async def delete_missing_media(media_id: int):
    """Remove a media entry from DB when Telegram says the source message is gone."""
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM media WHERE id = $1", media_id)
        await conn.execute("DELETE FROM user_history WHERE media_id = $1", media_id)
        await conn.execute(
            "UPDATE user_position SET current_media_id = NULL WHERE current_media_id = $1",
            media_id
        )
    logger.warning(f"🗑️ Removed missing media id={media_id} from DB")

async def _copy_media(bot: Bot, chat_id: int, media: dict) -> "telegram.Message":
    """Raw copy — raises TelegramError on failure."""
    return await bot.copy_message(
        chat_id=chat_id,
        from_chat_id=SOURCE_CHAT_ID,
        message_id=media["message_id"],
        caption="⏱️ 10 min mein delete ho jayega",
        reply_markup=media_keyboard(),
    )

async def send_media_to_user(
    bot: Bot,
    chat_id: int,
    media: dict,
    old_msg_id: int | None = None,
    is_prev: bool = False,
    user_id: int | None = None,
    _retries: int = 0,
) -> int | None:
    MAX_RETRIES = 10
    try:
        # Send new message FIRST for both prev & next — then delete old.
        # This way user never sees a blank/flash between messages.
        msg = await _copy_media(bot, chat_id, media)
        if old_msg_id:
            try:
                await bot.delete_message(chat_id=chat_id, message_id=old_msg_id)
            except TelegramError:
                pass
        _fire_and_forget(auto_delete(bot, chat_id, msg.message_id, AUTO_DELETE_SECONDS))
        return msg.message_id

    except TelegramError as e:
        err = str(e).lower()
        if "message to copy not found" in err or "message not found" in err:
            await delete_missing_media(media["id"])
            if _retries >= MAX_RETRIES or user_id is None:
                logger.error(f"❌ Max retries reached or no user_id for chat {chat_id}")
                return None
            next_media = await get_next_media(user_id)
            if not next_media:
                logger.warning(f"⚠️ No media left for chat {chat_id}")
                return None
            logger.info(f"🔄 Retry {_retries + 1}/{MAX_RETRIES} with media id={next_media['id']}")
            await mark_seen(user_id, next_media["id"])
            return await send_media_to_user(
                bot, chat_id, next_media,
                old_msg_id=old_msg_id,
                user_id=user_id,
                _retries=_retries + 1,
            )
        logger.error(f"send_media_to_user error: {e}")
        return None

async def auto_delete(bot: Bot, chat_id: int, message_id: int, delay: int):
    try:
        await asyncio.sleep(delay)
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except (TelegramError, asyncio.CancelledError):
        pass


# ─── Ban Check ─────────────────────────────────────────────────────────────────
async def check_ban(update: Update, ctx=None) -> bool:
    user_id  = update.effective_user.id
    username = update.effective_user.full_name or "Unknown"
    if await is_banned(user_id):
        expired  = await is_expired(user_id)
        msg_text = BUY_PREMIUM_MSG if expired else "🚫 *Aap ban ho gaye hain.*\nAdmin se contact karein."
        if update.message:
            await update.message.reply_text(msg_text, parse_mode="Markdown")
        elif update.callback_query:
            await update.callback_query.answer("💎 Premium expire! Admin se contact karein.", show_alert=True)
        # Notify admin with Approve / Keep Banned buttons
        if ctx:
            try:
                await ctx.bot.send_message(
                    chat_id=ADMIN_ID,
                    text=(
                        f"🔄 *Banned User Ne /start Kiya!*\n\n"
                        f"👤 Name: *{username}*\n"
                        f"🆔 User ID: `{user_id}`\n\n"
                        f"Subscription continue karna hai?"
                    ),
                    parse_mode="Markdown",
                    reply_markup=ban_request_keyboard(user_id),
                )
            except TelegramError:
                pass
        return True
    return False


# ─── /start ────────────────────────────────────────────────────────────────────
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id  = update.effective_user.id
    username = update.effective_user.full_name or "Unknown"
    if await check_ban(update, ctx):
        return
    await register_user(user_id)
    await update_last_seen(user_id)
    if await is_rejected(user_id):
        await update.message.reply_text("❌ *Aapki request reject ho gayi hai.*\n\nAdmin se contact karein.", parse_mode="Markdown")
        return
    if not await is_approved(user_id):
        await update.message.reply_text(
            "⏳ *Aapki request admin ke paas bhej di gayi hai.*\n\nApprove hone ke baad aap bot use kar sakte ho. Thoda wait karo! 🙏",
            parse_mode="Markdown"
        )
        try:
            await ctx.bot.send_message(
                chat_id=ADMIN_ID,
                text=f"🔔 *Naya User Request!*\n\n👤 Name: *{username}*\n🆔 User ID: `{user_id}`\n\nApprove ({APPROVAL_DAYS} Days) ya Reject karo:",
                parse_mode="Markdown",
                reply_markup=approval_keyboard(user_id),
            )
        except TelegramError as e:
            logger.error(f"Admin notify error: {e}")
        return

    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT expires_at FROM users WHERE user_id = $1", user_id)
    pos        = await get_position(user_id)
    expires_at = row["expires_at"] if row else None
    days_left  = None
    if expires_at:
        delta     = expires_at - datetime.utcnow()
        days_left = max(0, delta.days)
    exp_date_str = expires_at.strftime('%d %b %Y') if expires_at else "N/A"
    expiry_line  = f"📅 Expiry: *{exp_date_str}* ({days_left} din baaki)\n" if days_left is not None else ""

    if pos:
        welcome = f"👋 *Wapas aaye!*\n\n{expiry_line}\nWohi se shuru kar rahe hain jahan chhoda tha ⬇️"
    else:
        welcome = f"🎉 *Welcome!*\n\n{expiry_line}\n▶️ Next dabao aur enjoy karo!"
    await update.message.reply_text(welcome, parse_mode="Markdown")

    if pos and pos.get("current_media_id"):
        async with pool.acquire() as conn:
            media = await conn.fetchrow("SELECT id, message_id, media_type FROM media WHERE id = $1", pos["current_media_id"])
        if media:
            new_msg_id = await send_media_to_user(ctx.bot, user_id, dict(media), old_msg_id=pos.get("bot_message_id"), user_id=user_id)
            if new_msg_id:
                await save_position(user_id, media["id"], new_msg_id)
            return

    media = await get_next_media(user_id)
    if not media:
        await update.message.reply_text("⚠️ Abhi koi media available nahi hai.")
        return
    new_msg_id = await send_media_to_user(ctx.bot, user_id, media, user_id=user_id)
    if new_msg_id:
        await mark_seen(user_id, media["id"])
        await save_position(user_id, media["id"], new_msg_id)


# ─── /status ───────────────────────────────────────────────────────────────────
async def status_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if await is_banned(user_id):
        await update.message.reply_text(BUY_PREMIUM_MSG, parse_mode="Markdown")
        return
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT is_approved, expires_at FROM users WHERE user_id = $1", user_id)
    if not row or not row["is_approved"]:
        await update.message.reply_text("⏳ *Aapka account approved nahi hai abhi.*", parse_mode="Markdown")
        return
    expires_at = row["expires_at"]
    if expires_at:
        delta     = expires_at - datetime.utcnow()
        days_left = max(0, delta.days)
        exp_str   = expires_at.strftime('%d %b %Y')
        text = (
            f"📊 *Aapka Premium Status*\n\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"✅ Status: *Active*\n"
            f"📅 Expiry Date: *{exp_str}*\n"
            f"⏳ Baaki Din: *{days_left} din*\n"
            f"━━━━━━━━━━━━━━━━━━━━"
        )
    else:
        text = "✅ *Aapka account approved hai.*\n📅 Expiry: N/A"
    await update.message.reply_text(text, parse_mode="Markdown")


# ─── Button Handler ────────────────────────────────────────────────────────────
async def button_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query   = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    data    = query.data

    if data.startswith("approve_") or data.startswith("reject_") or data.startswith("keepban_"):
        if user_id != ADMIN_ID:
            await query.answer("🚫 Sirf admin ke liye!", show_alert=True)
            return
        target_id = int(data.split("_", 1)[1])
        if data.startswith("approve_"):
            expires = await approve_user(target_id)
            await query.edit_message_text(
                query.message.text + f"\n\n✅ *Approved!*\n📅 Expires: {expires.strftime('%d %b %Y')}",
                parse_mode="Markdown"
            )
            try:
                await ctx.bot.send_message(
                    target_id,
                    f"🎉 *Aapka access restore ho gaya!*\n\n📅 Expiry: *{expires.strftime('%d %b %Y')}*\n\n/start dabao aur enjoy karo 🚀",
                    parse_mode="Markdown"
                )
            except TelegramError:
                pass
        elif data.startswith("reject_"):
            await reject_user(target_id)
            await query.edit_message_text(query.message.text + "\n\n❌ *Rejected by admin.*", parse_mode="Markdown")
            try:
                await ctx.bot.send_message(target_id, "❌ *Aapki request reject ho gayi hai.*\n\nZyada jaankari ke liye admin se sampark karein.", parse_mode="Markdown")
            except TelegramError:
                pass
        elif data.startswith("keepban_"):
            await query.edit_message_text(query.message.text + "\n\n🚫 *Ban kept. No action taken.*", parse_mode="Markdown")
        return

    if data == "noop":
        return
    if await check_ban(update):
        return
    if not await is_approved(user_id):
        await query.answer("⏳ Aapki request abhi pending hai!", show_alert=True)
        return

    await update_last_seen(user_id)
    pos              = await get_position(user_id)
    current_media_id = pos["current_media_id"] if pos else None
    is_prev          = False

    if data == "next":
        media = await get_next_media(user_id)
        if not media:
            await query.message.reply_text("⚠️ Koi nai media nahi hai abhi.")
            return
        await mark_seen(user_id, media["id"])
    elif data == "prev":
        if not current_media_id:
            await query.answer("⚠️ Koi previous nahi hai!", show_alert=True)
            return
        prev = await get_prev_media(user_id, current_media_id)
        if not prev:
            await query.answer("⚠️ Ye pehli media hai!", show_alert=True)
            return
        media   = {"id": prev["media_id"], "message_id": prev["message_id"], "media_type": prev["media_type"]}
        is_prev = True
    else:
        return

    old_msg_id = pos["bot_message_id"] if pos else None
    new_msg_id = await send_media_to_user(ctx.bot, user_id, media, old_msg_id=old_msg_id, is_prev=is_prev, user_id=user_id)
    if new_msg_id:
        await save_position(user_id, media["id"], new_msg_id)


# ─── Media Watcher ─────────────────────────────────────────────────────────────
async def watcher(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg:
        return
    media_type = None
    if msg.video:
        media_type = "video"
    elif msg.photo:
        media_type = "photo"
    if not media_type:
        return
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO media (message_id, media_type) VALUES ($1, $2) ON CONFLICT (message_id) DO NOTHING",
            msg.message_id, media_type
        )
    logger.info(f"📥 New {media_type} saved — msg_id={msg.message_id}")


# ─── Admin: /stats ─────────────────────────────────────────────────────────────
async def stats_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Sirf admin use kar sakta hai.")
        return
    await update_last_seen(update.effective_user.id)
    async with pool.acquire() as conn:
        total_users    = await conn.fetchval("SELECT COUNT(*) FROM users")
        approved_count = await conn.fetchval("SELECT COUNT(*) FROM users WHERE is_approved = TRUE")
        banned_count   = await conn.fetchval("SELECT COUNT(*) FROM banned_users")
        media_count    = await conn.fetchval("SELECT COUNT(*) FROM media")
        pending_count  = await conn.fetchval("SELECT COUNT(*) FROM users WHERE is_approved = FALSE AND is_rejected = FALSE")
    text = (
        f"📊 *Bot Statistics*\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"👥 Total Joined: *{total_users}*\n"
        f"✅ Approved Users: *{approved_count}*\n"
        f"⏳ Pending Requests: *{pending_count}*\n"
        f"🚫 Banned Users: *{banned_count}*\n"
        f"🎬 Total Media in DB: *{media_count}*\n"
        f"━━━━━━━━━━━━━━━━━━━━"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


# ─── Admin: /broadcast ─────────────────────────────────────────────────────────
async def broadcast_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    3 ways to broadcast:
      1. Reply to ANY message → /broadcast        (copies it exactly, no forward label)
      2. /broadcast text (multiline ok)           (sends raw text as-is)
      3. Reply to photo → /broadcast caption      (photo + your caption)
    """
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Sirf admin use kar sakta hai.")
        return

    msg         = update.message
    replied_msg = msg.reply_to_message

    # Extract raw text after "/broadcast" — preserves newlines, spaces, emojis
    raw = msg.text or ""
    # Strip the command part (/broadcast or /broadcast@botname)
    if raw.startswith("/broadcast"):
        raw = raw.split(None, 1)[1] if len(raw.split(None, 1)) > 1 else ""
    raw = raw.strip()

    # ── Mode 1: Reply to any message + no text → copy message exactly ──────────
    if replied_msg and not raw:
        users = await get_all_active_users()
        if not users:
            await msg.reply_text("⚠️ Koi active user nahi hai.")
            return
        status_msg = await msg.reply_text(f"📤 Broadcasting to *{len(users)}* users...", parse_mode="Markdown")
        success, failed = 0, 0
        for uid in users:
            try:
                sent = await ctx.bot.copy_message(
                    chat_id=uid,
                    from_chat_id=update.effective_chat.id,
                    message_id=replied_msg.message_id,
                )
                _fire_and_forget(auto_delete(ctx.bot, uid, sent.message_id, 86400))
                success += 1
            except TelegramError:
                failed += 1
            await asyncio.sleep(0.05)
        await status_msg.edit_text(
            f"✅ *Broadcast Complete!*\n\n✔️ Sent: *{success}*\n❌ Failed: *{failed}*\n🕐 24 ghante baad auto-delete ho jayega.",
            parse_mode="Markdown"
        )
        return

    # ── Mode 2: Reply to photo + text → photo with exact caption ───────────────
    photo_file_id = None
    if replied_msg and replied_msg.photo and raw:
        photo_file_id = replied_msg.photo[-1].file_id

    # ── Nothing provided → show help ───────────────────────────────────────────
    if not photo_file_id and not raw:
        await msg.reply_text(
            "ℹ️ *Broadcast Usage:*\n\n"
            "1️⃣ *Exact copy:*\nKoi bhi message reply karo → `/broadcast`\n\n"
            "2️⃣ *Text:*\n`/broadcast Aapka message` _(multiline bhi kaam karta hai)_\n\n"
            "3️⃣ *Photo + caption:*\nPhoto reply karo → `/broadcast caption`",
            parse_mode="Markdown"
        )
        return

    users = await get_all_active_users()
    if not users:
        await msg.reply_text("⚠️ Koi active user nahi hai.")
        return

    status_msg = await msg.reply_text(f"📤 Broadcasting to *{len(users)}* users...", parse_mode="Markdown")
    success, failed = 0, 0

    # Preserve entities (bold/italic/links) from admin's message for text broadcasts
    entities = msg.entities or []
    # Shift entity offsets — remove the "/broadcast " prefix length
    prefix_len = len((msg.text or "").split(None, 1)[0]) + 1  # "/broadcast "
    shifted_entities = []
    for ent in entities:
        new_offset = ent.offset - prefix_len
        if new_offset >= 0:
            shifted_entities.append(
                ent.__class__(
                    type=ent.type,
                    offset=new_offset,
                    length=ent.length,
                    url=getattr(ent, "url", None),
                    user=getattr(ent, "user", None),
                    language=getattr(ent, "language", None),
                    custom_emoji_id=getattr(ent, "custom_emoji_id", None),
                )
            )

    for uid in users:
        try:
            if photo_file_id:
                sent = await ctx.bot.send_photo(
                    chat_id=uid,
                    photo=photo_file_id,
                    caption=raw,
                )
            else:
                sent = await ctx.bot.send_message(
                    chat_id=uid,
                    text=raw,
                    entities=shifted_entities if shifted_entities else None,
                )
            # Auto-delete broadcast after 24 hours silently
            _fire_and_forget(auto_delete(ctx.bot, uid, sent.message_id, 86400))
            success += 1
        except TelegramError:
            failed += 1
        await asyncio.sleep(0.05)

    await status_msg.edit_text(
        f"✅ *Broadcast Complete!*\n\n✔️ Sent: *{success}*\n❌ Failed: *{failed}*\n🕐 24 ghante baad auto-delete ho jayega.",
        parse_mode="Markdown"
    )


# ─── Admin: /ban ───────────────────────────────────────────────────────────────
async def ban_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Sirf admin use kar sakta hai.")
        return
    if not ctx.args:
        await update.message.reply_text("ℹ️ Usage: `/ban <user_id> [reason]`", parse_mode="Markdown")
        return
    try:
        target_id = int(ctx.args[0])
    except ValueError:
        await update.message.reply_text("❌ Invalid user ID.")
        return
    if target_id == ADMIN_ID:
        await update.message.reply_text("❌ Admin ko ban nahi kar sakte!")
        return
    reason = " ".join(ctx.args[1:]) if len(ctx.args) > 1 else "No reason given"
    await ban_user(target_id, reason)
    try:
        await ctx.bot.send_message(chat_id=target_id, text=f"🚫 *Aapko ban kar diya gaya hai.*\nReason: {reason}", parse_mode="Markdown")
    except TelegramError:
        pass
    await update.message.reply_text(f"✅ User `{target_id}` ban ho gaya!\n📝 Reason: {reason}", parse_mode="Markdown")


# ─── Admin: /unban ─────────────────────────────────────────────────────────────
async def unban_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Sirf admin use kar sakta hai.")
        return
    if not ctx.args:
        await update.message.reply_text("ℹ️ Usage: `/unban <user_id>`", parse_mode="Markdown")
        return
    try:
        target_id = int(ctx.args[0])
    except ValueError:
        await update.message.reply_text("❌ Invalid user ID.")
        return
    if not await is_banned(target_id):
        await update.message.reply_text(f"⚠️ User `{target_id}` ban nahi hai.", parse_mode="Markdown")
        return
    await unban_user(target_id)
    try:
        await ctx.bot.send_message(chat_id=target_id, text="✅ *Aapka ban hata diya gaya hai!*\n/start dabao aur enjoy karo 🎉", parse_mode="Markdown")
    except TelegramError:
        pass
    await update.message.reply_text(f"✅ User `{target_id}` unban ho gaya!", parse_mode="Markdown")


# ─── Admin: /banned ────────────────────────────────────────────────────────────
async def banned_list_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Sirf admin use kar sakta hai.")
        return
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT user_id, reason, banned_at FROM banned_users ORDER BY banned_at DESC")
    if not rows:
        await update.message.reply_text("✅ Koi bhi user ban nahi hai.")
        return
    lines = ["🚫 *Banned Users List:*\n"]
    for r in rows:
        lines.append(f"• `{r['user_id']}`\n  📝 {r['reason']}\n  🕐 {r['banned_at'].strftime('%d %b %Y, %H:%M')}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ─── Admin: /approve ───────────────────────────────────────────────────────────
async def approve_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Sirf admin use kar sakta hai.")
        return
    if not ctx.args:
        await update.message.reply_text("ℹ️ Usage: `/approve <user_id>`", parse_mode="Markdown")
        return
    try:
        target_id = int(ctx.args[0])
    except ValueError:
        await update.message.reply_text("❌ Valid User ID daalo.")
        return
    expires = await approve_user(target_id)
    await update.message.reply_text(f"✅ User `{target_id}` approve ho gaya!\n📅 Expiry: *{expires.strftime('%d %b %Y')}*", parse_mode="Markdown")
    try:
        await ctx.bot.send_message(target_id, f"🎉 *Aapka access restore ho gaya!*\n\n📅 Expiry: *{expires.strftime('%d %b %Y')}*\n\n/start dabao aur enjoy karo 🚀", parse_mode="Markdown")
    except TelegramError:
        pass


# ─── Admin: /reject ────────────────────────────────────────────────────────────
async def reject_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Sirf admin use kar sakta hai.")
        return
    if not ctx.args:
        await update.message.reply_text("ℹ️ Usage: `/reject <user_id>`", parse_mode="Markdown")
        return
    try:
        target_id = int(ctx.args[0])
    except ValueError:
        await update.message.reply_text("❌ Valid User ID daalo.")
        return
    await reject_user(target_id)
    await update.message.reply_text(f"❌ User `{target_id}` reject ho gaya!", parse_mode="Markdown")
    try:
        await ctx.bot.send_message(target_id, "❌ *Aapki request reject ho gayi hai.*\n\nAdmin se contact karein.", parse_mode="Markdown")
    except TelegramError:
        pass


# ─── Admin: /pending ───────────────────────────────────────────────────────────
async def pending_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Sirf admin use kar sakta hai.")
        return
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT user_id, joined_at FROM users WHERE is_approved = FALSE AND is_rejected = FALSE ORDER BY joined_at ASC")
    if not rows:
        await update.message.reply_text("✅ Koi pending request nahi hai.")
        return
    lines = [f"⏳ *Pending Requests ({len(rows)}):*\n"]
    for r in rows:
        lines.append(f"• `{r['user_id']}` — {r['joined_at'].strftime('%d %b, %H:%M')}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ─── Admin: /expiring ──────────────────────────────────────────────────────────
async def expiring_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Sirf admin use kar sakta hai.")
        return
    soon = datetime.utcnow() + timedelta(days=3)
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT user_id, expires_at FROM users WHERE is_approved = TRUE AND expires_at IS NOT NULL AND expires_at BETWEEN NOW() AND $1 ORDER BY expires_at ASC",
            soon
        )
    if not rows:
        await update.message.reply_text("✅ Agle 3 din mein koi expire nahi ho raha.")
        return
    lines = [f"⚠️ *Expiring in 3 Days ({len(rows)} users):*\n"]
    for r in rows:
        delta = r["expires_at"] - datetime.utcnow()
        lines.append(f"• `{r['user_id']}` — {r['expires_at'].strftime('%d %b %Y')} (*{delta.days}d baaki*)")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ─── Admin: /support ───────────────────────────────────────────────────────────
async def support_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    global support_button_enabled
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Sirf admin use kar sakta hai.")
        return
    arg = ctx.args[0].lower() if ctx.args else None
    if arg == "on":
        support_button_enabled = True
        await update.message.reply_text("✅ Support button *ON* kar diya gaya.", parse_mode="Markdown")
    elif arg == "off":
        support_button_enabled = False
        await update.message.reply_text("🚫 Support button *OFF* kar diya gaya.", parse_mode="Markdown")
    else:
        status = "ON ✅" if support_button_enabled else "OFF 🚫"
        await update.message.reply_text(
            f"🆘 *Support Button Status: {status}*\n\n"
            f"Toggle karne ke liye:\n`/support on` ya `/support off`",
            parse_mode="Markdown"
        )


# ─── Main ──────────────────────────────────────────────────────────────────────
def main():
    async def post_init(app: Application):
        await init_db()
        await set_bot_commands(app.bot)
        # Store task on app so it's tracked and cancelled cleanly on shutdown
        app.bot_data["expiry_task"] = asyncio.create_task(expiry_checker(app.bot))
        logger.info("⏰ Expiry checker started")

    async def post_shutdown(app: Application):
        task = app.bot_data.get("expiry_task")
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    app = Application.builder().token(BOT_TOKEN).post_init(post_init).post_shutdown(post_shutdown).build()
    app.add_handler(CommandHandler("start",     start))
    app.add_handler(CommandHandler("status",    status_cmd))
    app.add_handler(CommandHandler("stats",     stats_cmd))
    app.add_handler(CommandHandler("broadcast", broadcast_cmd))
    app.add_handler(CommandHandler("ban",       ban_cmd))
    app.add_handler(CommandHandler("unban",     unban_cmd))
    app.add_handler(CommandHandler("banned",    banned_list_cmd))
    app.add_handler(CommandHandler("approve",   approve_cmd))
    app.add_handler(CommandHandler("reject",    reject_cmd))
    app.add_handler(CommandHandler("pending",   pending_cmd))
    app.add_handler(CommandHandler("expiring",  expiring_cmd))
    app.add_handler(CommandHandler("support",   support_cmd))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.Chat(SOURCE_CHAT_ID) & (filters.VIDEO | filters.PHOTO), watcher))
    logger.info("🤖 Bot polling...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
