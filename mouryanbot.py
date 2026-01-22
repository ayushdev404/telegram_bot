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
from keep_alive import keep_alive

keep_alive()

# --- 1. SETUP & PATHS ---
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
os.chdir(SCRIPT_DIR)

# Suppress Pydantic warnings to keep logs clean
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

# --- 3. DEPENDENCIES ---
try:
    from dotenv import load_dotenv
    from aiogram import Bot, Dispatcher, F, types
    from aiogram.filters import Command, CommandStart, CommandObject
    from aiogram.types import Message
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

# --- 4. CONFIGURATION ---
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

# --- 5. DATABASE MANAGER ---
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
            # Files Table
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
            # Users Table
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    joined_at INTEGER
                )
            """)
            self.conn.execute("CREATE INDEX IF NOT EXISTS idx_unique_id ON files(file_unique_id)")

    async def execute(self, query: str, args: tuple = (), fetch: str = None):
        """Thread-safe async database execution."""
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

# --- 6. BOT INSTANCE (PYTHONANYWHERE PROXY SUPPORT) ---
session = None
if os.getenv('PYTHONANYWHERE_DOMAIN') or os.getenv('PYTHONANYWHERE_SITE'):
    # Automatically use PythonAnywhere's proxy if detected
    session = AiohttpSession(proxy="http://proxy.server:3128")
    logger.info("🔧 Configured PythonAnywhere Proxy")

bot = Bot(
    token=BOT_TOKEN,
    session=session,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML)
)
dp = Dispatcher()

# --- 7. HELPER FUNCTIONS ---
RATE_LIMIT = {}

def is_spamming(user_id: int) -> bool:
    """Basic rate limiter for text chat to prevent abuse."""
    now = time.time()
    last_time = RATE_LIMIT.get(user_id, 0)
    if now - last_time < 0.8: # 800ms limit between messages
        return True
    RATE_LIMIT[user_id] = now
    return False

async def track_user(user_id: int):
    """Add user to database for broadcasting."""
    await db.execute("INSERT OR IGNORE INTO users (user_id, joined_at) VALUES (?, ?)", (user_id, int(time.time())))

def get_file_info(msg: Message):
    """Extract file details from a message."""
    media = msg.document or msg.video or msg.photo or msg.audio
    if not media: return None

    if isinstance(media, list): media = media[-1] # High res photo

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

# --- 8. CORE HANDLERS ---

@dp.message(CommandStart())
async def start_handler(message: Message, command: CommandObject):
    await track_user(message.from_user.id)
    args = command.args

    # 1. Normal Welcome (No Code)
    if not args:
        return await message.answer(
            f"👋 <b>Hello {message.from_user.first_name}!</b>\n\n"
            "I am your Secure File Assistant.\n"
            "📂 <b>To get a file:</b> Send me the link.\n"
            "ℹ️ <b>Info:</b> Type /owner or /help."
        )

    # 2. File Retrieval (With Code)
    code = args
    data = await db.execute("SELECT * FROM files WHERE code=?", (code,), fetch='one')

    if not data:
        return await message.answer("❌ <b>Error:</b> File not found.")

    if not data['is_active']:
        return await message.answer("🔒 <b>Error:</b> File link is revoked.")

    try:
        await bot.send_chat_action(message.chat.id, action=ChatAction.UPLOAD_DOCUMENT)

        # Determine method based on type
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

# --- 9. OWNER & INFO HANDLERS ---

@dp.message(Command("owner"))
async def owner_command(message: Message):
    """Responds to /owner command."""
    await message.answer(
        "👤 <b>Owner Information</b>\n\n"
        "<b>Name:</b> Its Light\n"
        "<b>Telegram:</b> @OfficialItslightMourya\n"
        "<b>Email:</b> ayushmourya881@gmail.com\n\n"
        "<i>Feel free to contact for inquiries!</i>"
    )

@dp.message(Command("stats"))
async def stats_handler(message: Message):
    """Admin only stats."""
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
    """Admin only broadcast."""
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
        except Exception:
            failed += 1

    await status.edit_text(f"✅ <b>Done</b>\nSent: {sent}\nFailed/Blocked: {failed}")

# --- 10. INTELLIGENT CHAT HANDLER ---

@dp.message(F.text)
async def chat_intelligence(message: Message):
    """Handles text messages, keywords, greetings, and fallbacks."""

    # 1. Spam Check
    if is_spamming(message.from_user.id):
        return # Ignore spammers

    text = message.text.lower().strip()

    # 2. Owner Keywords
    owner_keywords = ["owner", "creator", "developer", "who made you", "admin info"]
    if any(k in text for k in owner_keywords):
        return await owner_command(message)

    # 3. Greetings & Appreciation
    greetings = ["hi", "hello", "hey", "good morning", "hola", "start"]
    if text in greetings:
        return await message.answer(f"👋 Hello <b>{message.from_user.first_name}</b>! How can I help you today?")

    if "thank" in text or "thx" in text:
        return await message.answer("😊 You're welcome! Happy to help.")

    if "help" in text:
        return await message.answer(
            "🛠 <b>Bot Help</b>\n\n"
            "• Send <b>/owner</b> for creator info.\n"
            "• If you have a file link, just send it here.\n"
            "• Admins can upload files by sending them."
        )

    # 4. Smart Fallback (Private Chat Only)
    if message.chat.type == "private":
        await message.answer(
            "🤖 <b>I didn't catch that.</b>\n\n"
            "I'm a file sharing assistant. You can:\n"
            "• Send /start to refresh\n"
            "• Send /owner for info\n"
            "• Send a file link to download"
        )

# --- 11. FILE UPLOAD (ADMIN ONLY) ---

@dp.message(F.document | F.video | F.photo | F.audio)
async def upload_handler(message: Message):
    if message.from_user.id != ADMIN_ID:
        # Ignore uploads from non-admins to prevent spam
        return

    status = await message.answer("⏳ <b>Securing File...</b>")

    try:
        info = get_file_info(message)
        if not info: return await status.edit_text("❌ Unknown media.")

        f_id, f_uid, f_type, f_name, cap = info

        # Deduplication
        exist = await db.execute("SELECT code FROM files WHERE file_unique_id=?", (f_uid,), fetch='one')

        if exist:
            code = exist['code']
            is_new = False
        else:
            code = uuid.uuid4().hex[:10]
            await db.execute(
                "INSERT INTO files VALUES (?, ?, ?, ?, ?, ?, ?, 0, 1)",
                (code, f_id, f_uid, f_type, f_name, cap, int(time.time()))
            )
            is_new = True

        link = f"https://t.me/{BOT_USERNAME}?start={code}"

        await status.edit_text(
            f"{'✅ <b>File Secured</b>' if is_new else 'ℹ️ <b>Already Exists</b>'}\n\n"
            f"📂 Type: {f_type}\n"
            f"🔗 Link:\n<code>{link}</code>"
        )
    except Exception as e:
        logger.error(f"Upload Error: {e}")
        await status.edit_text("❌ Database Error.")

# --- 12. RUNNER ---
async def main():
    print(f"✅ Bot Started: @{BOT_USERNAME}")
    # Drop pending to avoid notification flood
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Bot stopped.")
