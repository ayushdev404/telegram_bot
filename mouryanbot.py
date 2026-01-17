import logging
import os
import sys
import asyncio
import sqlite3
import time
import uuid
import atexit
import warnings
from typing import Tuple, Optional

# --- 1. SETUP & PATHS ---
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
os.chdir(SCRIPT_DIR)

# Suppress Pydantic warnings (Clean up logs)
warnings.filterwarnings("ignore", category=UserWarning, module="pydantic")

# --- 2. LOGGING ---
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("bot.log", mode="a", encoding="utf-8")
    ]
)
logger = logging.getLogger("MouryanBot")

# --- 3. DEPENDENCY CHECK ---
try:
    from dotenv import load_dotenv
    from aiogram import Bot, Dispatcher, F, types
    from aiogram.filters import Command, CommandStart, CommandObject
    from aiogram.types import Message, FSInputFile
    from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
    from aiogram.client.default import DefaultBotProperties
    from aiogram.client.session.aiohttp import AiohttpSession
    from aiogram.enums import ParseMode, ChatAction
except ImportError as e:
    logger.critical(f"Missing dependencies: {e}")
    logger.critical("Run: pip install -r requirements.txt")
    sys.exit(1)

# Load Environment
load_dotenv(os.path.join(SCRIPT_DIR, ".env"))

# --- 4. CONFIG ---
def get_env(key, required=True):
    val = os.getenv(key)
    if required and not val:
        logger.critical(f"Missing {key} in .env")
        sys.exit(1)
    return val

BOT_TOKEN = get_env("BOT_TOKEN")
try:
    ADMIN_ID = int(get_env("ADMIN_ID"))
except ValueError:
    logger.critical("ADMIN_ID must be an integer.")
    sys.exit(1)
BOT_USERNAME = get_env("BOT_USERNAME", "Bot").replace("@", "")

# --- 5. DATABASE ---
class Database:
    def __init__(self, db_name="database.db"):
        self.path = os.path.join(SCRIPT_DIR, db_name)
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.lock = asyncio.Lock()
        self.init_db()

    def init_db(self):
        with self.conn:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS files (
                    code TEXT PRIMARY KEY,
                    file_id TEXT NOT NULL,
                    file_unique_id TEXT NOT NULL,
                    file_type TEXT NOT NULL,
                    file_name TEXT,
                    caption TEXT,
                    created_at INTEGER,
                    downloads INTEGER DEFAULT 0,
                    is_active INTEGER DEFAULT 1
                )
            """)
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    joined_at INTEGER
                )
            """)
            self.conn.execute("CREATE INDEX IF NOT EXISTS idx_unique_id ON files(file_unique_id)")

    async def execute(self, query: str, args: tuple = (), fetch: str = None):
        async with self.lock:
            def _run():
                try:
                    cursor = self.conn.cursor()
                    cursor.execute(query, args)
                    if fetch == 'one': return cursor.fetchone()
                    if fetch == 'all': return cursor.fetchall()
                    self.conn.commit()
                except Exception as e:
                    logger.error(f"DB Error: {e}")
                    return None
            return await asyncio.to_thread(_run)

    def close(self):
        if self.conn: self.conn.close()

db = Database()
atexit.register(db.close)

# --- 6. BOT INSTANCE (PYTHONANYWHERE PROXY FIX) ---
session = None
if os.getenv('PYTHONANYWHERE_DOMAIN') or os.getenv('PYTHONANYWHERE_SITE'):
    # PythonAnywhere Free Tier requires this proxy
    session = AiohttpSession(proxy="http://proxy.server:3128")
    logger.info("🔧 Configured PythonAnywhere Proxy")

bot = Bot(
    token=BOT_TOKEN,
    session=session,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML)
)
dp = Dispatcher()

# --- 7. HELPER FUNCTIONS ---
async def track_user(user_id: int):
    await db.execute("INSERT OR IGNORE INTO users (user_id, joined_at) VALUES (?, ?)", (user_id, int(time.time())))

def get_file_info(msg: Message):
    media = msg.document or msg.video or msg.photo or msg.audio
    if not media: return None

    if isinstance(media, list): media = media[-1]

    f_id = media.file_id
    f_uid = media.file_unique_id
    f_name = getattr(media, "file_name", f"file_{f_uid[:8]}")

    if msg.photo: f_type = "photo"
    elif msg.video: f_type = "video"
    elif msg.document: f_type = "document"
    elif msg.audio: f_type = "audio"
    else: f_type = "unknown"

    caption = msg.caption or ""
    return f_id, f_uid, f_type, f_name, caption

# --- 8. HANDLERS ---
@dp.message(CommandStart())
async def start_handler(message: Message, command: CommandObject):
    await track_user(message.from_user.id)
    args = command.args

    if not args:
        return await message.answer(
            f"👋 <b>Hello {message.from_user.first_name}!</b>\n\n"
            "I am a secure file vault.\n"
            "Send me a link to get your file."
        )

    code = args
    data = await db.execute("SELECT * FROM files WHERE code=?", (code,), fetch='one')

    if not data:
        return await message.answer("❌ <b>Error:</b> File not found.")

    if not data['is_active']:
        return await message.answer("🔒 <b>Error:</b> File link is revoked.")

    try:
        await bot.send_chat_action(message.chat.id, action=ChatAction.UPLOAD_DOCUMENT)

        if data['file_type'] == "photo":
            await message.answer_photo(photo=data['file_id'], caption=data['caption'])
        elif data['file_type'] == "video":
            await message.answer_video(video=data['file_id'], caption=data['caption'])
        else:
            await message.answer_document(document=data['file_id'], caption=data['caption'])

        await db.execute("UPDATE files SET downloads = downloads + 1 WHERE code=?", (code,))

    except TelegramBadRequest:
        await message.answer("❌ <b>Telegram Error:</b> File deleted/expired.")
    except Exception as e:
        logger.error(f"Send Error: {e}")
        await message.answer("❌ Failed to send file.")

@dp.message(F.document | F.video | F.photo | F.audio)
async def upload_handler(message: Message):
    if message.from_user.id != ADMIN_ID:
        return

    status = await message.answer("⏳ <b>Processing...</b>")

    try:
        info = get_file_info(message)
        if not info: return await status.edit_text("❌ Unknown media.")

        f_id, f_uid, f_type, f_name, cap = info

        exist = await db.execute("SELECT code FROM files WHERE file_unique_id=?", (f_uid,), fetch='one')

        if exist:
            code = exist['code']
            new_file = False
        else:
            code = uuid.uuid4().hex[:10]
            await db.execute(
                "INSERT INTO files VALUES (?, ?, ?, ?, ?, ?, ?, 0, 1)",
                (code, f_id, f_uid, f_type, f_name, cap, int(time.time()))
            )
            new_file = True

        link = f"https://t.me/{BOT_USERNAME}?start={code}"

        await status.edit_text(
            f"{'✅ <b>New File</b>' if new_file else 'ℹ️ <b>File Exists</b>'}\n\n"
            f"📂 Type: {f_type}\n"
            f"🔗 Link:\n<code>{link}</code>"
        )
    except Exception as e:
        logger.error(f"Upload: {e}")
        await status.edit_text("❌ Error saving file.")

@dp.message(Command("stats"))
async def stats_handler(message: Message):
    if message.from_user.id != ADMIN_ID: return

    files = await db.execute("SELECT COUNT(*), SUM(downloads) FROM files", fetch='one')
    users = await db.execute("SELECT COUNT(*) FROM users", fetch='one')

    await message.answer(
        f"📊 <b>Stats</b>\n\n"
        f"👥 Users: {users[0]}\n"
        f"📂 Files: {files[0]}\n"
        f"⬇️ Downloads: {files[1] or 0}"
    )

@dp.message(Command("broadcast"))
async def broadcast_handler(message: Message):
    if message.from_user.id != ADMIN_ID: return
    if not message.reply_to_message:
        return await message.answer("⚠️ Reply to a message to broadcast.")

    status = await message.answer("📢 <b>Broadcasting...</b>")
    users = await db.execute("SELECT user_id FROM users", fetch='all')

    sent, failed = 0, 0

    for row in users:
        user_id = row['user_id']
        try:
            await bot.copy_message(
                chat_id=user_id,
                from_chat_id=message.chat.id,
                message_id=message.reply_to_message.message_id
            )
            sent += 1
            await asyncio.sleep(0.05)
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after)
            try:
                await bot.copy_message(chat_id=user_id, from_chat_id=message.chat.id, message_id=message.reply_to_message.message_id)
                sent += 1
            except: failed += 1
        except (TelegramForbiddenError, TelegramBadRequest):
            await db.execute("DELETE FROM users WHERE user_id=?", (user_id,))
            failed += 1
        except Exception as e:
            logger.error(f"Broadcast fail {user_id}: {e}")
            failed += 1

    await status.edit_text(f"✅ <b>Done</b>\nSent: {sent}\nFailed/Blocked: {failed}")

# --- 9. STARTUP ---
async def main():
    print(f"✅ Bot Started: @{BOT_USERNAME}")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Bot stopped.")