# countdown_bot_supabase.py
# Async Telegram countdown bot with Supabase persistence (Render-ready)
# Required env vars:
#   TELEGRAM_TOKEN
#   SUPABASE_URL          e.g., https://xyz.supabase.co
#   SUPABASE_KEY          (service_role key preferred)
#   SUPABASE_DB_URL       (Postgres connection string)
# Optional:
#   TARGET_CHAT           Telegram chat/channel id (e.g., -1001234567890)

import os
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, Any

from supabase import create_client, Client
from telegram import Bot
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# ---------------- CONFIG ----------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
TARGET_CHAT = os.getenv("TARGET_CHAT")  # optional

if not TELEGRAM_TOKEN or not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError("Missing required env vars: TELEGRAM_TOKEN, SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
countdown_tasks: Dict[int, asyncio.Task] = {}

# ---------------- UTILITIES ----------------
def utc_now() -> datetime:
    return datetime.now(timezone.utc)

def normalize_countdown(days: int, hours: int, minutes: int) -> timedelta:
    return timedelta(days=days, hours=hours, minutes=minutes)

def format_remaining(td: timedelta) -> str:
    if td.total_seconds() <= 0:
        return "⏰ Countdown finished!"
    total_minutes = int(td.total_seconds() // 60)
    days = total_minutes // (24 * 60)
    hours = (total_minutes % (24 * 60)) // 60
    minutes = total_minutes % 60
    return f"⏳ {days:02d}d : {hours:02d}h : {minutes:02d}m"

# ---------------- DATABASE ----------------
async def save_countdown(message_id: int, chat_id: str, end_time: datetime, post_text: str):
    supabase.table("countdowns").upsert({
        "message_id": message_id,
        "chat_id": str(chat_id),
        "end_time": end_time.isoformat(),
        "post_text": post_text,
    }).execute()

async def delete_countdown(message_id: int):
    supabase.table("countdowns").delete().eq("message_id", message_id).execute()

async def load_all_countdowns():
    result = supabase.table("countdowns").select("*").execute()
    return result.data or []

# ---------------- COUNTDOWN LOGIC ----------------
async def run_countdown(bot: Bot, chat_id: str, post_text: str, end_time: datetime, message_id: int):
    try:
        while True:
            now = utc_now()
            remaining = end_time - now
            text = f"{post_text}\n\n{format_remaining(remaining)}"
            try:
                await bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=text, parse_mode=ParseMode.HTML)
            except TelegramError as e:
                logging.warning(f"Edit failed: {e}")
            if remaining.total_seconds() <= 0:
                await delete_countdown(message_id)
                countdown_tasks.pop(message_id, None)
                break
            await asyncio.sleep(60)
    except asyncio.CancelledError:
        logging.info(f"Countdown task cancelled ({message_id})")

# ---------------- COMMANDS ----------------
async def start_countdown(update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if len(args) < 3:
        await update.message.reply_text("Usage: /start_countdown <days> <hours> <minutes> [message]")
        return

    try:
        days, hours, minutes = map(int, args[:3])
    except ValueError:
        await update.message.reply_text("Days, hours, and minutes must be integers.")
        return

    custom_text = " ".join(args[3:]) if len(args) > 3 else "Countdown"
    end_time = utc_now() + normalize_countdown(days, hours, minutes)
    chat_id = TARGET_CHAT or update.effective_chat.id
    bot = context.bot

    sent = await bot.send_message(chat_id=chat_id, text=custom_text, parse_mode=ParseMode.HTML)
    await save_countdown(sent.message_id, chat_id, end_time, custom_text)

    task = asyncio.create_task(run_countdown(bot, chat_id, custom_text, end_time, sent.message_id))
    countdown_tasks[sent.message_id] = task

async def stop_countdown(update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /stop_countdown <message_id>")
        return
    mid = int(context.args[0])
    task = countdown_tasks.pop(mid, None)
    if task:
        task.cancel()
    await delete_countdown(mid)
    await update.message.reply_text(f"Countdown {mid} stopped.")

# ---------------- STARTUP ----------------
async def reschedule_on_startup(app):
    bot = app.bot
    now = utc_now()
    rows = await load_all_countdowns()
    for r in rows:
        end_time = datetime.fromisoformat(r["end_time"])
        if end_time > now:
            task = asyncio.create_task(run_countdown(bot, r["chat_id"], r["post_text"], end_time, int(r["message_id"])))
            countdown_tasks[int(r["message_id"])] = task
        else:
            await delete_countdown(int(r["message_id"]))

# ---------------- MAIN ----------------
async def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    await reschedule_on_startup(app)
    app.add_handler(CommandHandler("start_countdown", start_countdown))
    app.add_handler(CommandHandler("stop_countdown", stop_countdown))
    await app.run_polling()

if __name__ == "__main__":
    asyncio.run(main())


# ---------------- GLOBAL STATE ----------------
countdown_tasks: Dict[int, asyncio.Task] = {}

# ---------------- UTILITIES ----------------
def normalize_countdown(days: int, hours: int, minutes: int) -> timedelta:
    total_minutes = minutes + hours * 60 + days * 24 * 60
    return timedelta(minutes=total_minutes)

def format_remaining(td: timedelta) -> str:
    if td.total_seconds() <= 0:
        return "⏰ Countdown finished!"
    total_minutes = int(td.total_seconds() // 60)
    days = total_minutes // (24 * 60)
    hours = (total_minutes % (24 * 60)) // 60
    minutes = total_minutes % 60
    return f"⏳ {days:02d}d : {hours:02d}h : {minutes:02d}m"

def utc_now() -> datetime:
    return datetime.now(timezone.utc)

# ---------------- DATABASE ----------------
CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS countdowns (
  message_id BIGINT PRIMARY KEY,
  chat_id TEXT NOT NULL,
  end_time TIMESTAMP WITH TIME ZONE NOT NULL,
  post_text TEXT NOT NULL,
  created_at TIMESTAMP WITH TIME ZONE DEFAULT now()
);
"""

async def init_db(pool: asyncpg.pool.Pool):
    async with pool.acquire() as conn:
        await conn.execute(CREATE_TABLE_SQL)

async def save_countdown(pool, message_id: int, chat_id: str, end_time: datetime, post_text: str):
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO countdowns (message_id, chat_id, end_time, post_text)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (message_id)
            DO UPDATE SET chat_id = EXCLUDED.chat_id, end_time = EXCLUDED.end_time, post_text = EXCLUDED.post_text
            """,
            message_id, str(chat_id), end_time, post_text
        )

async def delete_countdown(pool, message_id: int):
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM countdowns WHERE message_id=$1", message_id)

async def load_all_countdowns(pool):
    async with pool.acquire() as conn:
        return await conn.fetch("SELECT message_id, chat_id, end_time, post_text FROM countdowns")

# ---------------- COUNTDOWN LOOP ----------------
async def run_countdown(bot: Bot, pool, chat_id: str, post_text: str, end_time: datetime, message_id: int):
    try:
        while True:
            now = utc_now()
            remaining = end_time - now
            text = post_text + "\n\n" + format_remaining(remaining)

            try:
                await bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=text, parse_mode=ParseMode.HTML)
            except TelegramError as e:
                logging.warning(f"Edit failed for {message_id}: {e}")

            if remaining.total_seconds() <= 0:
                final_text = post_text + "\n\n⏰ Countdown finished!"
                try:
                    await bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=final_text, parse_mode=ParseMode.HTML)
                except TelegramError:
                    pass
                await delete_countdown(pool, message_id)
                countdown_tasks.pop(message_id, None)
                break

            # Sleep until next minute mark
            await asyncio.sleep(60 - now.second)
    except asyncio.CancelledError:
        logging.info(f"Countdown task cancelled ({message_id})")

# ---------------- COMMANDS ----------------
async def start_countdown(update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if len(args) < 3:
        await update.message.reply_text("Usage: /start_countdown <days> <hours> <minutes> [message]")
        return

    try:
        days, hours, minutes = map(int, args[:3])
    except ValueError:
        await update.message.reply_text("Days, hours, and minutes must be integers.")
        return

    custom_text = " ".join(args[3:]) if len(args) > 3 else "Countdown"
    delta = normalize_countdown(days, hours, minutes)
    if delta.total_seconds() <= 0:
        await update.message.reply_text("Please provide a positive duration.")
        return

    end_time = utc_now() + delta
    chat_id = TARGET_CHAT or update.effective_chat.id
    bot = context.bot
    initial_text = custom_text + "\n\n" + format_remaining(delta)

    try:
        sent = await bot.send_message(chat_id=chat_id, text=initial_text, parse_mode=ParseMode.HTML)
    except TelegramError as e:
        await update.message.reply_text(f"Failed to send message: {e}")
        return

    pool = context.bot_data["db_pool"]
    await save_countdown(pool, sent.message_id, chat_id, end_time, custom_text)

    task = context.application.create_task(run_countdown(bot, pool, chat_id, custom_text, end_time, sent.message_id))
    countdown_tasks[sent.message_id] = task

    await update.message.reply_text(f"Countdown started in chat {chat_id} (message_id={sent.message_id}).")

async def stop_countdown(update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 1:
        await update.message.reply_text("Usage: /stop_countdown <message_id>")
        return

    try:
        mid = int(context.args[0])
    except ValueError:
        await update.message.reply_text("message_id must be an integer.")
        return

    task = countdown_tasks.pop(mid, None)
    if task:
        task.cancel()

    pool = context.bot_data["db_pool"]
    await delete_countdown(pool, mid)
    await update.message.reply_text(f"Countdown {mid} stopped and removed.")

# ---------------- STARTUP ----------------
async def reschedule_on_startup(application):
    pool = application.bot_data["db_pool"]
    bot = application.bot
    now = utc_now()
    rows = await load_all_countdowns(pool)

    for r in rows:
        try:
            message_id = int(r["message_id"])
            chat_id = r["chat_id"]
            post_text = r["post_text"]
            end_time = r["end_time"]
            if not isinstance(end_time, datetime):
                end_time = datetime.fromisoformat(end_time)

            if end_time <= now:
                await bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=post_text + "\n\n⏰ Countdown finished!", parse_mode=ParseMode.HTML)
                await delete_countdown(pool, message_id)
                continue

            task = application.create_task(run_countdown(bot, pool, chat_id, post_text, end_time, message_id))
            countdown_tasks[message_id] = task
        except Exception as e:
            logging.warning(f"Skipping invalid row {r}: {e}")
            await delete_countdown(pool, r["message_id"])

# ---------------- MAIN ----------------
async def main():
    logging.info("Starting countdown bot...")
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    # Retry DB connection for Render cold starts
    pool = None
    for attempt in range(5):
        try:
            pool = await asyncpg.create_pool(dsn=SUPABASE_DB_URL, min_size=1, max_size=5)
            break
        except Exception as e:
            logging.warning(f"DB connection failed ({attempt+1}/5): {e}")
            await asyncio.sleep(5)

    if not pool:
        logging.error("Could not connect to Supabase after retries. Exiting.")
        return

    await init_db(pool)
    app.bot_data["db_pool"] = pool
    await reschedule_on_startup(app)

    app.add_handler(CommandHandler("start_countdown", start_countdown))
    app.add_handler(CommandHandler("stop_countdown", stop_countdown))

    logging.info("Bot is now polling...")
    await app.run_polling(close_loop=False)

if __name__ == "__main__":
    asyncio.run(main())
# If not provided, attempt to read from SUPABASE_URL and SUPABASE_KEY is used for REST only; require SUPABASE_DB_URL for DB access
if not SUPABASE_DB_URL:
    raise RuntimeError("Please set SUPABASE_DB_URL (Postgres connection string) as env var for direct DB access.")

# --- In-memory task store ---
countdown_tasks: Dict[int, asyncio.Task] = {}

# --- Utils ---
def normalize_countdown(days: int, hours: int, minutes: int) -> timedelta:
    total_minutes = minutes + hours * 60 + days * 24 * 60
    return timedelta(minutes=total_minutes)

def format_remaining(td: timedelta) -> str:
    if td.total_seconds() <= 0:
        return "⏰ Countdown finished!"
    total_minutes = int(td.total_seconds() // 60)
    days = total_minutes // (24 * 60)
    hours = (total_minutes % (24 * 60)) // 60
    minutes = total_minutes % 60
    return f"⏳ {days:02d}d : {hours:02d}h : {minutes:02d}m"

def utc_now() -> datetime:
    return datetime.now(timezone.utc)

# --- Database helpers ---
CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS countdowns (
  message_id BIGINT PRIMARY KEY,
  chat_id TEXT NOT NULL,
  end_time TIMESTAMP WITH TIME ZONE NOT NULL,
  post_text TEXT NOT NULL,
  created_at TIMESTAMP WITH TIME ZONE DEFAULT now()
);
"""

async def init_db(pool: asyncpg.pool.Pool):
    async with pool.acquire() as conn:
        await conn.execute(CREATE_TABLE_SQL)

async def save_countdown(pool: asyncpg.pool.Pool, message_id: int, chat_id: str, end_time: datetime, post_text: str):
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO countdowns(message_id, chat_id, end_time, post_text) VALUES($1,$2,$3,$4) ON CONFLICT (message_id) DO UPDATE SET chat_id=EXCLUDED.chat_id, end_time=EXCLUDED.end_time, post_text=EXCLUDED.post_text",
            message_id, str(chat_id), end_time, post_text
        )

async def delete_countdown(pool: asyncpg.pool.Pool, message_id: int):
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM countdowns WHERE message_id=$1", message_id)

async def load_all_countdowns(pool: asyncpg.pool.Pool):
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT message_id, chat_id, end_time, post_text FROM countdowns")
        return rows

# --- Background updater ---
async def run_countdown(bot: Bot, pool: asyncpg.pool.Pool, chat_id: str, post_text: str, end_time: datetime, message_id: int):
    try:
        while True:
            now = utc_now()
            remaining = end_time - now
            text = post_text + "\n\n" + format_remaining(remaining)
            try:
                await bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=text, parse_mode=ParseMode.HTML)
            except TelegramError:
                logging.exception("Edit failed (ignored).")
            if remaining.total_seconds() <= 0:
                final_text = post_text + "\n\n" + "⏰ Countdown finished!"
                try:
                    await bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=final_text, parse_mode=ParseMode.HTML)
                except TelegramError:
                    logging.exception("Final edit failed.")
                try:
                    await delete_countdown(pool, message_id)
                except Exception:
                    logging.exception("DB delete failed.")
                countdown_tasks.pop(message_id, None)
                break
            # align to next minute
            seconds_to_next_minute = 60 - now.second
            await asyncio.sleep(seconds_to_next_minute)
    except asyncio.CancelledError:
        logging.info("Task cancelled for %s", message_id)
        return

# --- Handlers ---
async def start_countdown(update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if len(args) < 3:
        await update.message.reply_text("Usage: /start_countdown <days> <hours> <minutes> [message]")
        return
    try:
        days = int(args[0]); hours = int(args[1]); minutes = int(args[2])
    except ValueError:
        await update.message.reply_text("Days/hours/minutes must be integers.")
        return
    custom_text = " ".join(args[3:]) if len(args) > 3 else "Countdown"
    delta = normalize_countdown(days, hours, minutes)
    if delta.total_seconds() <= 0:
        await update.message.reply_text("Please provide a positive duration.")
        return
    end_time = utc_now() + delta

    chat_id = TARGET_CHAT or update.effective_chat.id
    initial_text = custom_text + "\n\n" + format_remaining(delta)
    bot = context.bot
    try:
        sent = await bot.send_message(chat_id=chat_id, text=initial_text, parse_mode=ParseMode.HTML)
    except TelegramError as e:
        await update.message.reply_text(f"Failed to send message: {e}")
        return

    pool: asyncpg.pool.Pool = context.bot_data["db_pool"]
    await save_countdown(pool, sent.message_id, chat_id, end_time, custom_text)

    task = context.application.create_task(run_countdown(bot, pool, chat_id, custom_text, end_time, sent.message_id))
    countdown_tasks[sent.message_id] = task

    await update.message.reply_text(f"Countdown started (message_id={sent.message_id}).")

async def stop_countdown(update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 1:
        await update.message.reply_text("Usage: /stop_countdown <message_id>")
        return
    try:
        mid = int(context.args[0])
    except ValueError:
        await update.message.reply_text("message_id must be integer.")
        return
    task = countdown_tasks.get(mid)
    if task:
        task.cancel()
        countdown_tasks.pop(mid, None)
    pool: asyncpg.pool.Pool = context.bot_data["db_pool"]
    try:
        await delete_countdown(pool, mid)
    except Exception:
        logging.exception("DB delete failed.")
    await update.message.reply_text(f"Countdown {mid} cancelled and removed.")

# --- Startup/resume ---
async def reschedule_on_startup(application):
    pool: asyncpg.pool.Pool = application.bot_data["db_pool"]
    bot = application.bot
    now = utc_now()
    rows = await load_all_countdowns(pool)
    for r in rows:
        try:
            message_id = int(r["message_id"])
            chat_id = r["chat_id"]
            post_text = r["post_text"]
            end_time = r["end_time"]
            if isinstance(end_time, datetime):
                et = end_time
            else:
                et = end_time.replace(tzinfo=timezone.utc)
            if et <= now:
                try:
                    await bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=post_text + "\n\n⏰ Countdown finished!", parse_mode=ParseMode.HTML)
                except TelegramError:
                    pass
                await delete_countdown(pool, message_id)
                continue
            task = application.create_task(run_countdown(bot, pool, chat_id, post_text, et, message_id))
            countdown_tasks[message_id] = task
        except Exception:
            logging.exception("Skipping malformed row; attempting delete.")
            try:
                await delete_countdown(pool, r["message_id"])
            except Exception:
                pass

# --- Main ---
def main():
    logging.basicConfig(level=logging.INFO)
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    # create DB pool and store in bot_data
    async def startup():
        pool = await asyncpg.create_pool(dsn=SUPABASE_DB_URL, min_size=1, max_size=5)
        await init_db(pool)
        app.bot_data["db_pool"] = pool
        await reschedule_on_startup(app)

    app.post_init(startup)
    app.add_handler(CommandHandler("start_countdown", start_countdown))
    app.add_handler(CommandHandler("stop_countdown", stop_countdown))

    app.run_polling()

if __name__ == "__main__":
    main()
