import os
import asyncio
import random
import logging
from datetime import datetime, timedelta

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, Bot
)
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes
)
from telegram.error import TelegramError

import asyncpg

BOT_TOKEN        = os.environ["BOT_TOKEN"]
DATABASE_URL     = os.environ["DATABASE_URL"]
SOURCE_CHAT_ID   = int(os.environ["SOURCE_CHAT_ID"])
ADMIN_ID         = int(os.environ["ADMIN_ID"])
ADMIN_USERNAME   = os.environ.get("ADMIN_USERNAME", "@SynaX_69")

AUTO_DELETE_SECONDS = 600
REPEAT_CHANCE       = 0.10
APPROVAL_DAYS       = 28

BUY_PREMIUM_MSG = (
    "💎 *Aapki Premium Access Expire Ho Gayi!*\n\n"
    "━━━━━━━━━━━━━━━━━━━━\n"
    "🚫 Aapke *28 din* poore ho gaye hain.\n\n"
    "🔓 *Premium Access Kaise Lein?*\n"
    "👉 Admin se sampark karein aur apna subscription renew karein.\n\n"
    "📩 Admin: @SynaX_69\n"
    "━━━━━━━━━━━━━━━━━━━━\n"
    "✨ _Premium members ko unlimited access milta hai!_"
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

pool: asyncpg.Pool = None

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
    logger.info("✅ Database initialized")


async def expiry_checker(bot: Bot):
    while True:
        try:
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
                await ban_user(uid, reason="28-day premium expired")
                async with pool.acquire() as conn:
                    await conn.execute("UPDATE users SET is_approved = FALSE WHERE user_id = $1", uid)
                try:
                    await bot.send_message(chat_id=uid, text=BUY_PREMIUM_MSG, parse_mode="Markdown")
                except TelegramError:
                    pass
                logger.info(f"⏰ Auto-banned expired user: {uid}")
        except Exception as e:
            logger.error(f"Expiry checker error: {e}")
        await asyncio.sleep(3600)


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
        # Check karo existing expiry
        row = await conn.fetchrow("SELECT expires_at FROM users WHERE user_id = $1", user_id)
        existing_expiry = row["expires_at"] if row else None

        # Agar expiry abhi baaki hai toh naya time mat do
        if existing_expiry and existing_expiry > datetime.utcnow():
            expires = existing_expiry
        else:
            expires = datetime.utcnow() + timedelta(days=APPROVAL_DAYS)

        await conn.execute("""
            UPDATE users SET is_approved = TRUE, is_rejected = FALSE,
            approved_at = NOW(), expires_at = $2 WHERE user_id = $1
        """, user_id, expires)
        # ✅ Ban bhi hatao automatically
        await conn.execute("DELETE FROM banned_users WHERE user_id = $1", user_id)
    return expires

async def reject_user(user_id: int):
    async with pool.acquire() as conn:
        await conn.execute("UPDATE users SET is_approved = FALSE, is_rejected = TRUE WHERE user_id = $1", user_id)

def approval_keyboard(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Approve (28 Days)", callback_data=f"approve_{user_id}"),
        InlineKeyboardButton("❌ Reject",            callback_data=f"reject_{user_id}"),
    ]])

async def get_stats() -> dict:
    async with pool.acquire() as conn:
        total       = await conn.fetchval("SELECT COUNT(*) FROM users")
        live_cutoff = datetime.utcnow() - timedelta(minutes=5)
        live        = await conn.fetchval("SELECT COUNT(*) FROM users WHERE last_seen >= $1", live_cutoff)
    return {"total": total or 0, "live": live or 0}

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

def media_keyboard(stats: dict) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅️ Previous", callback_data="prev"), InlineKeyboardButton("▶️ Next", callback_data="next")],
        [InlineKeyboardButton(f"🟢 Live: {stats['live']}", callback_data="noop"), InlineKeyboardButton(f"👥 Joined: {stats['total']}", callback_data="noop")]
    ])

async def send_media_to_user(bot: Bot, chat_id: int, media: dict, stats: dict, old_msg_id: int | None = None) -> int | None:
    keyboard = media_keyboard(stats)
    try:
        if old_msg_id:
            try:
                await bot.delete_message(chat_id=chat_id, message_id=old_msg_id)
            except TelegramError:
                pass
        msg = await bot.copy_message(
            chat_id=chat_id,
            from_chat_id=SOURCE_CHAT_ID,
            message_id=media["message_id"],
            caption="⏱️ 10 min mein delete ho jayega",
            reply_markup=keyboard,
            
        )
        asyncio.create_task(auto_delete(bot, chat_id, msg.message_id, AUTO_DELETE_SECONDS))
        return msg.message_id
    except TelegramError as e:
        logger.error(f"send_media_to_user error: {e}")
        return None

async def auto_delete(bot: Bot, chat_id: int, message_id: int, delay: int):
    await asyncio.sleep(delay)
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except TelegramError:
        pass

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
        # ✅ Har banned user ke liye admin ko notify karo with buttons
        if ctx:
            try:
                await ctx.bot.send_message(
                    chat_id=ADMIN_ID,
                    text=(
                        f"🔄 *Access Request!*\n\n"
                        f"👤 Name: *{username}*\n"
                        f"🆔 User ID: `{user_id}`\n\n"
                        f"Access dena hai?"
                    ),
                    parse_mode="Markdown",
                    reply_markup=approval_keyboard(user_id)
                )
            except TelegramError:
                pass
        return True
    return False

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
        await update.message.reply_text("⏳ *Aapki request admin ke paas bhej di gayi hai.*\n\nApprove hone ke baad aap bot use kar sakte ho. Thoda wait karo! 🙏", parse_mode="Markdown")
        try:
            await ctx.bot.send_message(
                chat_id=ADMIN_ID,
                text=f"🔔 *Naya User Request!*\n\n👤 Name: *{username}*\n🆔 User ID: `{user_id}`\n\nApprove (28 Days) ya Reject karo:",
                parse_mode="Markdown",
                reply_markup=approval_keyboard(user_id)
            )
        except TelegramError as e:
            logger.error(f"Admin notify error: {e}")
        return
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT expires_at FROM users WHERE user_id = $1", user_id)
    stats     = await get_stats()
    pos       = await get_position(user_id)
    expires_at = row["expires_at"] if row else None
    days_left  = None
    if expires_at:
        delta     = expires_at - datetime.utcnow()
        days_left = max(0, delta.days)
    expiry_line = f"⏳ Access: *{days_left} din baaki*\n" if days_left is not None else ""
    if pos:
        welcome = f"👋 *Wapas aaye!*\n\n🟢 Live: *{stats['live']}* | 👥 Joined: *{stats['total']}*\n{expiry_line}\nWohi se shuru kar rahe hain jahan chhoda tha ⬇️"
    else:
        welcome = f"🎉 *Welcome!*\n\n🟢 Live: *{stats['live']}* | 👥 Joined: *{stats['total']}*\n{expiry_line}\n▶️ Next dabao aur enjoy karo!"
    await update.message.reply_text(welcome, parse_mode="Markdown")
    if pos and pos.get("current_media_id"):
        async with pool.acquire() as conn:
            media = await conn.fetchrow("SELECT id, message_id, media_type FROM media WHERE id = $1", pos["current_media_id"])
        if media:
            new_msg_id = await send_media_to_user(ctx.bot, user_id, dict(media), stats, old_msg_id=pos.get("bot_message_id"))
            if new_msg_id:
                await save_position(user_id, media["id"], new_msg_id)
            return
    media = await get_next_media(user_id)
    if not media:
        await update.message.reply_text("⚠️ Abhi koi media available nahi hai.")
        return
    new_msg_id = await send_media_to_user(ctx.bot, user_id, media, stats)
    if new_msg_id:
        await mark_seen(user_id, media["id"])
        await save_position(user_id, media["id"], new_msg_id)

async def button_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query   = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    data    = query.data
    if data.startswith("approve_") or data.startswith("reject_"):
        if user_id != ADMIN_ID:
            await query.answer("🚫 Sirf admin ke liye!", show_alert=True)
            return
        target_id = int(data.split("_")[1])
        if data.startswith("approve_"):
            expires = await approve_user(target_id)
            await query.edit_message_text(query.message.text + f"\n\n✅ *Approved!*\n📅 Expires: {expires.strftime('%d %b %Y')}", parse_mode="Markdown")
            try:
                await ctx.bot.send_message(target_id, f"🎉 *Aapka access restore ho gaya!*\n\n📅 Expiry: *{expires.strftime('%d %b %Y')}*\n\n/start dabao aur enjoy karo 🚀", parse_mode="Markdown")
            except TelegramError:
                pass
        elif data.startswith("reject_"):
            await reject_user(target_id)
            await query.edit_message_text(query.message.text + "\n\n❌ *Rejected by admin.*", parse_mode="Markdown")
            try:
                await ctx.bot.send_message(target_id, "❌ *Aapki request reject ho gayi hai.*\n\nZyada jaankari ke liye admin se sampark karein.", parse_mode="Markdown")
            except TelegramError:
                pass
        return
    if data == "noop":
        return
    if await check_ban(update):
        return
    if not await is_approved(user_id):
        await query.answer("⏳ Aapki request abhi pending hai!", show_alert=True)
        return
    await update_last_seen(user_id)
    stats            = await get_stats()
    pos              = await get_position(user_id)
    current_media_id = pos["current_media_id"] if pos else None
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
        media = {"id": prev["media_id"], "message_id": prev["message_id"], "media_type": prev["media_type"]}
    old_msg_id = pos["bot_message_id"] if pos else None
    new_msg_id = await send_media_to_user(ctx.bot, user_id, media, stats, old_msg_id=old_msg_id)
    if new_msg_id:
        await save_position(user_id, media["id"], new_msg_id)

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
        await conn.execute("INSERT INTO media (message_id, media_type) VALUES ($1, $2) ON CONFLICT (message_id) DO NOTHING", msg.message_id, media_type)
    logger.info(f"📥 New {media_type} saved — msg_id={msg.message_id}")

async def stats_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update_last_seen(update.effective_user.id)
    stats = await get_stats()
    async with pool.acquire() as conn:
        media_count    = await conn.fetchval("SELECT COUNT(*) FROM media")
        approved_count = await conn.fetchval("SELECT COUNT(*) FROM users WHERE is_approved = TRUE")
        banned_count   = await conn.fetchval("SELECT COUNT(*) FROM banned_users")
        expired_count  = await conn.fetchval("SELECT COUNT(*) FROM users WHERE expires_at IS NOT NULL AND expires_at < NOW()")
    text = (f"📊 *Bot Stats*\n\n🟢 Live users (last 5 min): *{stats['live']}*\n👥 Total joined: *{stats['total']}*\n✅ Approved users: *{approved_count}*\n🚫 Banned users: *{banned_count}*\n⏰ Expired users: *{expired_count}*\n🎬 Total media in DB: *{media_count}*")
    await update.message.reply_text(text, parse_mode="Markdown")

async def broadcast_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Sirf admin use kar sakta hai.")
        return
    if not ctx.args:
        await update.message.reply_text("ℹ️ Usage: `/broadcast Aapka message yahan`", parse_mode="Markdown")
        return
    message_text = " ".join(ctx.args)
    users = await get_all_active_users()
    if not users:
        await update.message.reply_text("⚠️ Koi active user nahi hai.")
        return
    status_msg = await update.message.reply_text(f"📤 Broadcasting to *{len(users)}* users...", parse_mode="Markdown")
    success, failed = 0, 0
    for uid in users:
        try:
            await ctx.bot.send_message(chat_id=uid, text=f"📢 *Admin Message:*\n\n{message_text}", parse_mode="Markdown")
            success += 1
        except TelegramError:
            failed += 1
        await asyncio.sleep(0.05)
    await status_msg.edit_text(f"✅ *Broadcast Complete!*\n\n✔️ Sent: *{success}*\n❌ Failed: *{failed}*", parse_mode="Markdown")

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

async def expiring_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Sirf admin use kar sakta hai.")
        return
    soon = datetime.utcnow() + timedelta(days=3)
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT user_id, expires_at FROM users WHERE is_approved = TRUE AND expires_at IS NOT NULL AND expires_at BETWEEN NOW() AND $1 ORDER BY expires_at ASC", soon)
    if not rows:
        await update.message.reply_text("✅ Agle 3 din mein koi expire nahi ho raha.")
        return
    lines = [f"⚠️ *Expiring in 3 Days ({len(rows)} users):*\n"]
    for r in rows:
        delta = r["expires_at"] - datetime.utcnow()
        lines.append(f"• `{r['user_id']}` — {r['expires_at'].strftime('%d %b %Y')} (*{delta.days}d baaki*)")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

def main():
    async def post_init(app: Application):
        await init_db()
        asyncio.create_task(expiry_checker(app.bot))
        logger.info("⏰ Expiry checker started")

    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start",     start))
    app.add_handler(CommandHandler("stats",     stats_cmd))
    app.add_handler(CommandHandler("broadcast", broadcast_cmd))
    app.add_handler(CommandHandler("ban",       ban_cmd))
    app.add_handler(CommandHandler("unban",     unban_cmd))
    app.add_handler(CommandHandler("banned",    banned_list_cmd))
    app.add_handler(CommandHandler("approve",   approve_cmd))
    app.add_handler(CommandHandler("reject",    reject_cmd))
    app.add_handler(CommandHandler("pending",   pending_cmd))
    app.add_handler(CommandHandler("expiring",  expiring_cmd))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.Chat(SOURCE_CHAT_ID) & (filters.VIDEO | filters.PHOTO), watcher))
    logger.info("🤖 Bot polling...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
