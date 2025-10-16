# countdown_bot_supabase.py
# Async Telegram bot persisting countdowns in Supabase (Postgres).
# Env vars required:
#   TELEGRAM_TOKEN
#   SUPABASE_URL    (e.g., https://xyz.supabase.co)
#   SUPABASE_KEY    (service_role key recommended for DB writes)
# Optional:
#   TARGET_CHAT
# Notes: updates once per minute, persists to Supabase so tasks resume after restart.

import os
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, Any

import asyncpg
from telegram import Bot
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# --- Config from env ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
SUPABASE_URL = os.getenv("https://speowxmjrizpxksmusau.supabase.co")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
TARGET_CHAT = os.getenv("TARGET_CHAT")  # optional
UPDATE_INTERVAL_SECONDS = 60

if not TELEGRAM_TOKEN or not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError("TELEGRAM_TOKEN, SUPABASE_URL, and SUPABASE_KEY must be set as env vars.")

# Derive Postgres connection info from SUPABASE_URL and SUPABASE_KEY
# Supabase exposes Postgres via a connection string you can find in Project Settings -> Database -> Connection Pooling
# But we can construct from environment if you prefer to supply SUPABASE_DB_URL directly.
SUPABASE_DB_URL = os.getenv("postgresql://postgres:dYSb7LS8vRngSHUj@db.speowxmjrizpxksmusau.supabase.co:5432/postgres")  # optional full pg connection string
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
