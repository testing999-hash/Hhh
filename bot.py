import asyncio
import html
import logging
import os
import re
import sqlite3
from datetime import datetime

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode, ContentType
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardMarkup, KeyboardButton
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

# ============================================================
# CONFIG
# ============================================================
# Keep credentials outside the source file. Set BOT_TOKEN and ADMIN_ID in Termux.
BOT_TOKEN = os.getenv("BOT_TOKEN", "8879794188:AAFE6eQMO1TnF0qEs8v79Rxz_1RWaMQLEBE").strip()
if not BOT_TOKEN:
    raise SystemExit("BOT_TOKEN is missing. Set BOT_TOKEN in your environment before starting the bot.")
try:
    ADMIN_ID = int(os.getenv("ADMIN_ID", "8754050004").strip())
except ValueError:
    raise SystemExit("ADMIN_ID must be a numeric Telegram user ID.")

DB_NAME = "tarun.db"

# Empty by default: the admin can add a custom /start message from the UI.
START_TEXT = ""

# Initial channel buttons. Admin can add as many as needed from /admin.
DEFAULT_URLS = {
    1: "https://t.me/",
    2: "https://t.me/",
    3: "https://t.me/",
    4: "https://t.me/",
    5: "https://t.me/",
    6: "https://t.me/",
}

VERIFY_TEXT = "✅ VERIFY ACCESS & GET DOWNLOAD"


# ============================================================
# DATABASE
# ============================================================
db = sqlite3.connect(DB_NAME, check_same_thread=False)
db.row_factory = sqlite3.Row

def db_init():
    db.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            blocked INTEGER DEFAULT 0,
            verified INTEGER DEFAULT 0,
            joined_at TEXT
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS channels (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            url TEXT NOT NULL,
            kind TEXT NOT NULL DEFAULT 'public',
            chat_id INTEGER
        )
    """)
    # Migrate older databases without deleting saved channels.
    channel_columns = {row[1] for row in db.execute("PRAGMA table_info(channels)").fetchall()}
    if "kind" not in channel_columns:
        db.execute("ALTER TABLE channels ADD COLUMN kind TEXT NOT NULL DEFAULT 'public'")
    if "chat_id" not in channel_columns:
        db.execute("ALTER TABLE channels ADD COLUMN chat_id INTEGER")
    db.commit()

    # Do not seed placeholder channel URLs. Admin should add real public
    # channel links from /admin; empty placeholders confuse membership checks.
    # Preserve any existing channels/settings from an older database.
    existing_start = db.execute("SELECT value FROM settings WHERE key=?", ("start_text",)).fetchone()
    if existing_start is None:
        db.execute("INSERT INTO settings(key,value) VALUES(?,?)", ("start_text", START_TEXT))
    elif (
        existing_start["value"] == "P2077KNG APK + KEY FREE"
        or "WELCOME TO TARUNX FILES" in existing_start["value"]
        or "TARUNX     HUB" in existing_start["value"]
        or "Yahan jo bhi Banda login Karen uska naam Aana chahie" in existing_start["value"]
        or "Tarun owner" in existing_start["value"].lower()
        or "tarun" in existing_start["value"].lower()
        or "𝐖ᴇʟᴄᴏᴍᴇ" in existing_start["value"]
    ):
        # Clear the old built-in welcome text once. A new message entered
        # later through the admin UI will be preserved on future restarts.
        db.execute("UPDATE settings SET value=? WHERE key=?", ("", "start_text"))
    db.execute(
        "INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)",
        ("verify_text", VERIFY_TEXT)
    )
    db.commit()


def setting(key, default=""):
    row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key, value):
    db.execute(
        "INSERT INTO settings(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value)
    )
    db.commit()


def get_channels():
    return db.execute(
        "SELECT id, title, url, kind, chat_id FROM channels ORDER BY id ASC"
    ).fetchall()


def add_channel(title, url, kind="public", chat_id=None):
    db.execute(
        "INSERT INTO channels(title, url, kind, chat_id) VALUES(?, ?, ?, ?)",
        (title, url, kind, chat_id)
    )
    db.commit()


def update_channel(channel_id, title, url, kind="public", chat_id=None):
    db.execute(
        "UPDATE channels SET title=?, url=?, kind=?, chat_id=? WHERE id=?",
        (title, url, kind, chat_id, channel_id)
    )
    db.commit()


def delete_channel(channel_id):
    db.execute("DELETE FROM channels WHERE id=?", (channel_id,))
    db.commit()


def add_user(message: Message):
    u = message.from_user
    db.execute("""
        INSERT INTO users(user_id,username,first_name,joined_at)
        VALUES(?,?,?,?)
        ON CONFLICT(user_id) DO UPDATE SET
            username=excluded.username,
            first_name=excluded.first_name
    """, (
        u.id,
        u.username or "",
        u.first_name or "",
        datetime.now().isoformat(timespec="seconds")
    ))
    db.commit()


def is_blocked(user_id):
    row = db.execute(
        "SELECT blocked FROM users WHERE user_id=?", (user_id,)
    ).fetchone()
    return bool(row and row["blocked"])


def set_block(user_id, value):
    db.execute(
        "UPDATE users SET blocked=? WHERE user_id=?",
        (1 if value else 0, user_id)
    )
    db.commit()


def total_users():
    return db.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]


# ============================================================
# BOT
# ============================================================
if not BOT_TOKEN or ADMIN_ID <= 0:
    raise SystemExit(
        "Missing configuration. Set both values before starting (never share your bot token):\n"
        "export BOT_TOKEN='PASTE_NEW_BOT_TOKEN_HERE'\n"
        "export ADMIN_ID='YOUR_NUMERIC_TELEGRAM_USER_ID'\n"
        "python promo-1.py"
    )

bot = Bot(
    BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML)
)
dp = Dispatcher(storage=MemoryStorage())
db_init()


# ============================================================
# FSM
# ============================================================
class AdminState(StatesGroup):
    broadcast = State()
    block = State()
    unblock = State()
    set_url = State()
    add_channel = State()
    edit_channel = State()
    set_video = State()
    set_voice = State()
    set_start_text = State()
    set_download_link = State()
    set_file = State()
    set_key = State()


# ============================================================
# KEYBOARDS
# ============================================================
def main_keyboard():
    rows = []
    channels = [
        channel for channel in get_channels()
        if channel["url"].strip().rstrip("/") not in ("https://t.me", "http://t.me", "https://telegram.me", "http://telegram.me")
    ]

    # Premium-style compact grid: two channel buttons per row.
    for i in range(0, len(channels), 2):
        row = []
        for channel in channels[i:i + 2]:
            row.append(
                InlineKeyboardButton(
                    text=channel["title"],
                    url=channel["url"]
                )
            )
        rows.append(row)

    rows.append([
        InlineKeyboardButton(
            text=setting("verify_text", VERIFY_TEXT),
            callback_data="verify"
        )
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📣 BROADCAST", callback_data="a_broadcast"),
            InlineKeyboardButton(text="👥 USER STATS", callback_data="a_total"),
        ],
        [
            InlineKeyboardButton(text="🚫 BLOCK USER", callback_data="a_block"),
            InlineKeyboardButton(text="✅ UNBLOCK USER", callback_data="a_unblock"),
        ],
        [
            InlineKeyboardButton(text="🔗 CHANNEL MANAGER", callback_data="a_urls"),
            InlineKeyboardButton(text="➕ ADD CHANNEL", callback_data="a_add_channel"),
        ],
        [
            InlineKeyboardButton(text="📝 WELCOME UI", callback_data="a_start_text"),
            InlineKeyboardButton(text="🌐 SET DOWNLOAD LINK", callback_data="a_download_link"),
        ],
        [
            InlineKeyboardButton(text="📦 SET DOWNLOAD FILE", callback_data="a_file"),
            InlineKeyboardButton(text="🔐 SET KEY", callback_data="a_key"),
        ],
        [
            InlineKeyboardButton(text="🎬 SET VIDEO", callback_data="a_video"),
        ],
        [
            InlineKeyboardButton(text="🎙 SET VOICE", callback_data="a_voice"),
        ],
        [
            InlineKeyboardButton(text="❌ CLOSE", callback_data="a_close")
        ]
    ])


# ============================================================
# START / MAIN SCREEN
# ============================================================
async def send_main(message: Message):
    add_user(message)

    if is_blocked(message.from_user.id):
        return

    # Optional admin-controlled video
    video_id = setting("video_id")
    if video_id:
        try:
            video_caption = setting("video_caption", START_TEXT)
            display_name = html.escape((message.from_user.full_name or message.from_user.first_name or "User"))
            video_caption = video_caption.replace("{name}", display_name)
            await message.answer_video(
                video=video_id,
                caption=video_caption,
            )
        except Exception:
            pass

    text = setting("start_text", START_TEXT).strip()
    if text:
        # A custom welcome message is optional and can be set from the admin UI.
        display_name = html.escape((message.from_user.full_name or message.from_user.first_name or "User"))
        text = text.replace("{name}", display_name)
        text = re.sub(r"(?i)tarun\s+owner", lambda _: display_name, text)
        await message.answer(text, reply_markup=main_keyboard())
    else:
        # No default welcome text: show only the channel/verification buttons.
        await message.answer("\u2060", reply_markup=main_keyboard())

    # Optional admin-controlled voice
    voice_id = setting("voice_id")
    if voice_id:
        try:
            await message.answer_voice(voice=voice_id)
        except Exception:
            pass


@dp.message(CommandStart())
async def start_handler(message: Message):
    await send_main(message)


# ============================================================
# VERIFY
# ============================================================
def username_from_url(url):
    """Return a public channel username, or None for invite/folder links."""
    m = re.match(r"^https?://(?:t\.me|telegram\.me)/([A-Za-z0-9_]+)(?:/\d+)?/?$", url.strip(), re.I)
    return "@" + m.group(1) if m else None


async def verify_public_channels(user_id):
    """Verify configured channels. Folder links are join buttons, not verifiable IDs."""
    checked = 0
    for channel in get_channels():
        kind = channel["kind"] or "public"
        if kind == "folder":
            continue
        if kind in ("private", "private_group") and channel["chat_id"] is not None:
            target = channel["chat_id"]
        else:
            target = username_from_url(channel["url"])
        if not target:
            return False, checked, "invalid_channel_url"
        checked += 1
        try:
            member = await bot.get_chat_member(target, user_id)
        except Exception:
            logging.exception("Could not check membership for %s", target)
            return False, checked, "channel_check_failed"
        status = getattr(member.status, "value", member.status)
        if status in ("left", "kicked"):
            return False, checked, "not_member"
        if status == "restricted" and not getattr(member, "is_member", False):
            return False, checked, "not_member"
    if checked == 0:
        return False, 0, "no_verifiable_channels"
    return True, checked, None


@dp.callback_query(F.data == "verify")
async def verify_handler(callback: CallbackQuery):
    uid = callback.from_user.id

    if is_blocked(uid):
        await callback.answer("You are blocked.", show_alert=True)
        return

    ok, checked, problem = await verify_public_channels(uid)

    if not ok:
        if problem in ("no_channels_configured", "no_verifiable_channels"):
            message = "⚠️ No verifiable channels are configured yet. Folder links can be opened, but need channel IDs to verify membership. Please contact the admin."
        elif problem == "channel_check_failed":
            message = "⚠️ I could not check channel membership. The bot must be added to each channel (preferably as an admin). Please contact the admin."
        elif problem == "invalid_channel_url":
            message = "⚠️ A channel link is invalid or is a private invite link that cannot be checked automatically. Please contact the admin."
        else:
            message = "🔒 ACCESS LOCKED\n\n📢 Join every channel below, then tap VERIFY ACCESS again."
        await callback.answer(message, show_alert=True)
        return

    db.execute("UPDATE users SET verified=1 WHERE user_id=?", (uid,))
    db.commit()

    await callback.answer("✅ Verified!", show_alert=True)

    # Deliver the admin-configured key/text, media, uploaded file, and/or URL.
    result_text = setting("verified_text", "").strip()
    if result_text:
        await callback.message.answer(result_text)

    result_video = setting("verified_video_id")
    result_voice = setting("verified_voice_id")
    if result_video:
        try:
            await callback.message.answer_video(video=result_video)
        except Exception:
            logging.exception("Could not send configured verification video")
    if result_voice:
        try:
            await callback.message.answer_voice(voice=result_voice)
        except Exception:
            logging.exception("Could not send configured verification voice")

    file_id = setting("download_file_id", "").strip()
    file_type = setting("download_file_type", "document")
    if file_id:
        try:
            sender = {
                "document": callback.message.answer_document,
                "video": callback.message.answer_video,
                "audio": callback.message.answer_audio,
                "voice": callback.message.answer_voice,
                "animation": callback.message.answer_animation,
                "photo": callback.message.answer_photo,
            }.get(file_type, callback.message.answer_document)
            await sender(file_id, caption="📥 Your download file is ready!")
        except Exception:
            logging.exception("Could not send configured download file")
            await callback.message.answer("⚠️ File delivery failed. Please contact the admin.")

    download_link = setting("download_link", "").strip()
    if download_link:
        await callback.message.answer(
            "💎 <b>ACCESS VERIFIED SUCCESSFULLY</b> 💎\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "📥 Your download link is ready.\n"
            "👇 Tap the button below to continue.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="🚀 DOWNLOAD NOW", url=download_link)
            ]])
        )
    if not (result_text or result_video or result_voice or file_id or download_link):
        await callback.message.answer("✅ Verification complete, but the admin has not configured a key, file, or download link yet.")


# ============================================================
# ADMIN PANEL
# ============================================================
@dp.message(Command("admin"))
async def admin_command(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    await message.answer(
        "💎 <b>PREMIUM CONTROL CENTER</b> 💎\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "⚙️ Manage your promo bot from the buttons below.\n"
        "📥 Set any supported download file or external link.\n"
        "🔑 Configure the key/message delivered after verification.\n"
        "━━━━━━━━━━━━━━━━━━",
        reply_markup=admin_keyboard()
    )


@dp.callback_query(F.data == "a_close")
async def admin_close(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
    await callback.message.delete()
    await callback.answer()


@dp.callback_query(F.data == "a_total")
async def admin_total(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
    await callback.answer()
    await callback.message.answer(
        f"👥 <b>Total Users:</b> {total_users()}"
    )


@dp.callback_query(F.data == "a_start_text")
async def admin_start_text_edit(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        return
    await state.set_state(AdminState.set_start_text)
    await callback.answer()
    current = setting("start_text", START_TEXT)
    await callback.message.answer(
        "✏️ <b>EDIT /START MESSAGE</b>\\n\\n"
        "Ab woh text bhejo jo users ko /start karne par channel buttons ke upar dikhna chahiye.\\n"
        "HTML formatting supported hai.\\n\\n"
        f"<b>Current text:</b>\\n{current}\\n\\n"
        "Cancel karne ke liye /cancel bhejo."
    )


@dp.message(AdminState.set_start_text)
async def save_start_text(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    if message.text and message.text.strip().lower() == "/cancel":
        await state.clear()
        await message.answer("❌ Start message edit cancelled.", reply_markup=admin_keyboard())
        return
    if not message.text or not message.text.strip():
        await message.answer("❌ Please non-empty text bhejo, ya /cancel.")
        return
    set_setting("start_text", message.text)
    await state.clear()
    await message.answer(
        "✅ <b>/start message updated!</b>\\nAb naye /start par ye text channel buttons ke upar dikhega.",
        reply_markup=admin_keyboard()
    )


@dp.callback_query(F.data == "a_download_link")
async def admin_download_link_start(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        return
    await state.set_state(AdminState.set_download_link)
    await callback.answer()
    current = setting("download_link", "")
    await callback.message.answer(
        "📥 <b>SET DOWNLOAD LINK</b>\n\n"
        "Woh direct download URL bhejo jo user ko channel verification ke baad milega.\n"
        "HTTP/HTTPS link bhejo. Link hatane ke liye <code>remove</code> likho.\n\n"
        f"Current link: {current or 'Not set'}\n\n"
        "Cancel: /cancel"
    )


@dp.message(AdminState.set_download_link)
async def save_download_link(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    raw = (message.text or "").strip()
    if raw.lower() == "/cancel":
        await state.clear()
        await message.answer("❌ Download link setup cancelled.", reply_markup=admin_keyboard())
        return
    if raw.lower() == "remove":
        set_setting("download_link", "")
        await state.clear()
        await message.answer("✅ Download link removed.", reply_markup=admin_keyboard())
        return
    if not re.match(r"^https?://\S+$", raw):
        await message.answer("❌ Valid HTTP/HTTPS URL bhejo, ya remove / /cancel likho.")
        return
    set_setting("download_link", raw)
    await state.clear()
    await message.answer(
        "✅ <b>Download link saved!</b>\nUsers ko successful channel verification ke baad DOWNLOAD NOW button milega.",
        reply_markup=admin_keyboard()
    )


@dp.callback_query(F.data == "a_broadcast")
async def admin_broadcast_start(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        return
    await state.set_state(AdminState.broadcast)
    await callback.answer()
    await callback.message.answer(
        "📢 <b>Broadcast Mode</b>\n\n"
        "Ab koi bhi <b>text / photo / video / voice / document</b> bhejo.\n"
        "Wahi message sab users ko forward/copy hoga.\n\n"
        "❌ Cancel: /cancel"
    )


@dp.message(AdminState.broadcast)
async def admin_broadcast_message(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return

    if message.text and message.text.strip().lower() == "/cancel":
        await state.clear()
        await message.answer("❌ Broadcast cancelled.", reply_markup=admin_keyboard())
        return

    users = db.execute("SELECT user_id FROM users WHERE blocked=0").fetchall()
    sent = 0
    failed = 0

    status = await message.answer("📢 Broadcasting...")

    for row in users:
        uid = row["user_id"]
        try:
            # copy_to preserves text/media without showing "Forwarded from".
            await message.copy_to(uid)
            sent += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.04)

    await status.edit_text(
        f"✅ <b>Broadcast Done</b>\n\n"
        f"👥 Users: {len(users)}\n"
        f"✅ Sent: {sent}\n"
        f"❌ Failed: {failed}"
    )
    await state.clear()


@dp.callback_query(F.data == "a_block")
async def admin_block_start(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        return
    await state.set_state(AdminState.block)
    await callback.answer()
    await callback.message.answer(
        "🚫 User ID bhejo jise block karna hai.\n"
        "Example: <code>123456789</code>\n\n"
        "/cancel"
    )


@dp.message(AdminState.block)
async def admin_block_user(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    if message.text and message.text.lower() == "/cancel":
        await state.clear()
        await message.answer("Cancelled.", reply_markup=admin_keyboard())
        return

    try:
        uid = int(message.text.strip())
    except Exception:
        await message.answer("❌ Valid numeric User ID bhejo.")
        return

    row = db.execute("SELECT user_id FROM users WHERE user_id=?", (uid,)).fetchone()
    if not row:
        # Create the row so an admin can block a user before their next message.
        db.execute(
            "INSERT INTO users(user_id,joined_at,blocked) VALUES(?,?,1)",
            (uid, datetime.now().isoformat(timespec="seconds"))
        )
        db.commit()
    else:
        set_block(uid, True)

    await state.clear()
    await message.answer(
        f"🚫 <b>Blocked:</b> <code>{uid}</code>",
        reply_markup=admin_keyboard()
    )


@dp.callback_query(F.data == "a_unblock")
async def admin_unblock_start(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        return
    await state.set_state(AdminState.unblock)
    await callback.answer()
    await callback.message.answer(
        "✅ User ID bhejo jise unblock karna hai.\n"
        "Example: <code>123456789</code>\n\n"
        "/cancel"
    )


@dp.message(AdminState.unblock)
async def admin_unblock_user(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    if message.text and message.text.lower() == "/cancel":
        await state.clear()
        await message.answer("Cancelled.", reply_markup=admin_keyboard())
        return

    try:
        uid = int(message.text.strip())
    except Exception:
        await message.answer("❌ Valid numeric User ID bhejo.")
        return

    set_block(uid, False)
    await state.clear()
    await message.answer(
        f"✅ <b>Unblocked:</b> <code>{uid}</code>",
        reply_markup=admin_keyboard()
    )


# ============================================================
def parse_channel_input(raw):
    """Parse public/private channels, groups and Telegram folder invite links."""
    parts = [x.strip() for x in raw.strip().split("|")]
    if len(parts) < 2 or len(parts) > 4:
        return (False, None, None, None, None)
    title, url = parts[0], parts[1]
    if not title or not re.match(r"^https?://(?:t\.me|telegram\.me)/\S+$", url, re.I):
        return (False, None, None, None, None)
    chat_id = None
    if "/addlist/" in url.lower() and len(parts) == 2:
        kind = "folder"
    elif re.match(r"^https?://(?:t\.me|telegram\.me)/(?:\+|joinchat/)", url, re.I):
        if len(parts) == 3 and re.fullmatch(r"-100\d+", parts[2]):
            kind, chat_id = "private", int(parts[2])  # legacy format
        elif len(parts) == 4 and parts[2].lower() in ("private", "private_channel", "private_group") and re.fullmatch(r"-100\d+", parts[3]):
            kind = "private_group" if parts[2].lower() == "private_group" else "private"
            chat_id = int(parts[3])
        else:
            return (False, None, None, None, None)
    elif username_from_url(url):
        if len(parts) == 2:
            kind = "public"
        elif len(parts) == 3 and parts[2].lower() in ("public_group", "group"):
            kind = "public_group"
        elif len(parts) == 3 and parts[2].lower() in ("public", "channel"):
            kind = "public"
        else:
            return (False, None, None, None, None)
    else:
        return (False, None, None, None, None)
    return (True, title, url, kind, chat_id)


# ADMIN CHANNEL CONTROL
# ============================================================
@dp.callback_query(F.data == "a_urls")
async def admin_urls(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
    await callback.answer()

    channels = get_channels()
    text = "<b>🔗 CHANNEL BUTTONS</b>\n\n"
    if not channels:
        text += "No channels added yet.\n"
    else:
        for ch in channels:
            extra = f" | {ch['kind']}" + (f" | ID: <code>{ch['chat_id']}</code>" if ch['chat_id'] is not None else "")
            text += f"🆔 <code>{ch['id']}</code>  <b>{html.escape(ch['title'])}</b>{extra}\n{html.escape(ch['url'])}\n\n"

    rows = []
    for ch in channels:
        rows.append([
            InlineKeyboardButton(
                text=f"✏️ {ch['title']}",
                callback_data=f"edit_ch:{ch['id']}"
            ),
            InlineKeyboardButton(
                text="🗑 DELETE",
                callback_data=f"del_ch:{ch['id']}"
            )
        ])
    rows.append([
        InlineKeyboardButton(text="➕ ADD CHANNEL", callback_data="a_add_channel")
    ])
    rows.append([
        InlineKeyboardButton(text="↩️ ADMIN", callback_data="back_admin")
    ])

    await callback.message.answer(
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows)
    )


@dp.callback_query(F.data == "a_add_channel")
async def admin_add_channel_start(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        return
    await state.set_state(AdminState.add_channel)
    await callback.answer()
    await callback.message.answer(
        "➕ <b>ADD CHANNEL BUTTON</b>\n\n"
        "Ek line mein in formats mein se koi bhejo:\n\n"
        "1) Public channel: <code>Name | https://t.me/channelname</code>\n"
        "2) Public group: <code>Name | https://t.me/groupname | public_group</code>\n"
        "3) Private channel: <code>Name | https://t.me/+INVITE | private | -100CHATID</code>\n"
        "4) Private group: <code>Name | https://t.me/+INVITE | private_group | -100CHATID</code>\n"
        "5) Telegram folder: <code>Name | https://t.me/addlist/CODE</code>\n\n"
        "Private channel/group membership check ke liye bot ko us chat mein add karo (preferably admin). Folder sirf join/open button hai; folder membership verify nahi hoti.\n"
        "❌ /cancel"
    )


@dp.message(AdminState.add_channel)
async def save_new_channel(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return

    if message.text and message.text.strip().lower() == "/cancel":
        await state.clear()
        await message.answer("❌ Cancelled.", reply_markup=admin_keyboard())
        return

    parsed = parse_channel_input(message.text or "")
    if not parsed[0]:
        await message.answer("❌ Format/link invalid hai. Admin instructions mein diye gaye 5 formats use karo.")
        return
    title, url, kind, chat_id = parsed[1], parsed[2], parsed[3], parsed[4]

    add_channel(title, url, kind, chat_id)
    await state.clear()
    await message.answer(
        f"✅ <b>Channel added!</b>\n\n"
        f"🔘 {html.escape(title)} ({kind})\n🔗 <code>{html.escape(url)}</code>" + (f"\n🆔 <code>{chat_id}</code>" if chat_id else ""),
        reply_markup=admin_keyboard()
    )


@dp.callback_query(F.data.regexp(r"^edit_ch:\d+$"))
async def admin_edit_channel_start(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        return
    channel_id = int(callback.data.split(":")[1])
    row = db.execute(
        "SELECT id, title, url FROM channels WHERE id=?", (channel_id,)
    ).fetchone()
    if not row:
        await callback.answer("Channel not found.", show_alert=True)
        return

    await state.set_state(AdminState.edit_channel)
    await state.update_data(channel_id=channel_id)
    await callback.answer()
    await callback.message.answer(
        f"✏️ <b>EDIT CHANNEL {channel_id}</b>\n\n"
        f"Current: {html.escape(row['title'])}\n"
        f"<code>{row['url']}</code>\n\n"
        "New format options:\n"
        "Public channel: <code>Name | https://t.me/channel</code>\n"
        "Public group: <code>Name | https://t.me/group | public_group</code>\n"
        "Private channel: <code>Name | https://t.me/+INVITE | private | -100ID</code>\n"
        "Private group: <code>Name | https://t.me/+INVITE | private_group | -100ID</code>\n"
        "Folder: <code>Name | https://t.me/addlist/CODE</code>\n"
        "❌ /cancel"
    )


@dp.message(AdminState.edit_channel)
async def save_edited_channel(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return

    if message.text and message.text.strip().lower() == "/cancel":
        await state.clear()
        await message.answer("❌ Cancelled.", reply_markup=admin_keyboard())
        return

    parsed = parse_channel_input(message.text or "")
    if not parsed[0]:
        await message.answer("❌ Format/link invalid hai. Admin instructions mein diye gaye formats use karo.")
        return
    title, url, kind, chat_id = parsed[1], parsed[2], parsed[3], parsed[4]

    data = await state.get_data()
    update_channel(data["channel_id"], title, url, kind, chat_id)
    await state.clear()
    await message.answer("✅ Channel updated.", reply_markup=admin_keyboard())


@dp.callback_query(F.data.regexp(r"^del_ch:\d+$"))
async def admin_delete_channel(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
    channel_id = int(callback.data.split(":")[1])
    row = db.execute(
        "SELECT title FROM channels WHERE id=?", (channel_id,)
    ).fetchone()
    if not row:
        await callback.answer("Channel not found.", show_alert=True)
        return

    delete_channel(channel_id)
    await callback.answer(f"Deleted: {html.escape(row['title'])}", show_alert=True)
    await admin_urls(callback)


# ============================================================
# ADMIN DOWNLOAD FILE + KEY CONTROL
# ============================================================
@dp.callback_query(F.data == "a_file")
async def admin_file_start(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        return
    await state.set_state(AdminState.set_file)
    await callback.answer()
    await callback.message.answer(
        "📦 <b>SET DOWNLOAD FILE</b>\n\n"
        "Ab koi bhi file bhejo: APK, ZIP, PDF, document, video, audio, photo ya animation. "
        "User channel verification ke baad ye file receive karega.\n\n"
        "Purani file hatane ke liye <code>remove</code> likho. Cancel: /cancel"
    )


@dp.message(AdminState.set_file)
async def save_download_file(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    raw = (message.text or "").strip().lower()
    if raw == "/cancel":
        await state.clear()
        await message.answer("❌ File setup cancelled.", reply_markup=admin_keyboard())
        return
    if raw == "remove":
        set_setting("download_file_id", "")
        set_setting("download_file_type", "document")
        await state.clear()
        await message.answer("✅ Download file removed.", reply_markup=admin_keyboard())
        return

    file_id = None
    file_type = None
    if message.document:
        file_id, file_type = message.document.file_id, "document"
    elif message.video:
        file_id, file_type = message.video.file_id, "video"
    elif message.audio:
        file_id, file_type = message.audio.file_id, "audio"
    elif message.voice:
        file_id, file_type = message.voice.file_id, "voice"
    elif message.animation:
        file_id, file_type = message.animation.file_id, "animation"
    elif message.photo:
        file_id, file_type = message.photo[-1].file_id, "photo"

    if not file_id:
        await message.answer("❌ File/media bhejo, ya remove / /cancel likho.")
        return
    set_setting("download_file_id", file_id)
    set_setting("download_file_type", file_type)
    await state.clear()
    await message.answer(
        f"✅ Download file saved!\nType: <code>{file_type}</code>\n"
        "Ye successful verification ke baad users ko bheji jayegi.",
        reply_markup=admin_keyboard()
    )


@dp.callback_query(F.data == "a_key")
async def admin_key_start(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        return
    await state.set_state(AdminState.set_key)
    await callback.answer()
    current = setting("verified_text", "")
    await callback.message.answer(
        "🔑 <b>SET KEY / VERIFICATION MESSAGE</b>\n\n"
        "Woh key ya text bhejo jo user ko verify hone ke baad milega. HTML formatting supported hai.\n"
        "Key hatane ke liye <code>remove</code> likho. Cancel: /cancel\n\n"
        f"Current: {current or 'Not set'}"
    )


@dp.message(AdminState.set_key)
async def save_key(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    raw = (message.text or "").strip()
    if raw.lower() == "/cancel":
        await state.clear()
        await message.answer("❌ Key setup cancelled.", reply_markup=admin_keyboard())
        return
    set_setting("verified_text", "" if raw.lower() == "remove" else raw)
    await state.clear()
    await message.answer(
        "✅ Key removed." if raw.lower() == "remove" else "✅ Key / verification message saved!",
        reply_markup=admin_keyboard()
    )


# ============================================================
# ADMIN VIDEO / VOICE CONTROL
# ============================================================
@dp.callback_query(F.data == "a_video")
async def admin_video_start(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        return
    await state.set_state(AdminState.set_video)
    await callback.answer()
    await callback.message.answer(
        "🎬 Ab ek video send karo.\n"
        "Ye /start par users ko dikhega.\n\n"
        "Caption bhi video ke saath bhej sakte ho.\n"
        "/cancel"
    )


@dp.message(AdminState.set_video, F.content_type == ContentType.VIDEO)
async def save_video(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return

    set_setting("video_id", message.video.file_id)
    set_setting("video_caption", message.caption or START_TEXT)
    await state.clear()

    await message.answer(
        "✅ Start video saved.",
        reply_markup=admin_keyboard()
    )


@dp.message(AdminState.set_video)
async def video_wrong_type(message: Message, state: FSMContext):
    if message.from_user.id == ADMIN_ID:
        if message.text and message.text.strip().lower() == "/cancel":
            await state.clear()
            await message.answer("❌ Video setup cancelled.", reply_markup=admin_keyboard())
            return
        await message.answer("❌ Please video bhejo, ya /cancel.")


@dp.callback_query(F.data == "a_voice")
async def admin_voice_start(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        return
    await state.set_state(AdminState.set_voice)
    await callback.answer()
    await callback.message.answer(
        "🎙 Ab voice/audio as a Telegram voice message send karo.\n"
        "Ye /start par users ko dikhega.\n\n"
        "/cancel"
    )


@dp.message(AdminState.set_voice, F.content_type == ContentType.VOICE)
async def save_voice(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return

    set_setting("voice_id", message.voice.file_id)
    await state.clear()

    await message.answer(
        "✅ Start voice saved.",
        reply_markup=admin_keyboard()
    )


@dp.message(AdminState.set_voice)
async def voice_wrong_type(message: Message, state: FSMContext):
    if message.from_user.id == ADMIN_ID:
        if message.text and message.text.strip().lower() == "/cancel":
            await state.clear()
            await message.answer("❌ Voice setup cancelled.", reply_markup=admin_keyboard())
            return
        await message.answer("❌ Please Telegram voice message bhejo, ya /cancel.")


# ============================================================
# ADMIN BACK
# ============================================================
@dp.callback_query(F.data == "back_admin")
async def back_admin(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
    await callback.answer()
    await callback.message.edit_text(
        "<b>⚙️ ADMIN PANEL</b>",
        reply_markup=admin_keyboard()
    )


# ============================================================
# CANCEL
# ============================================================
@dp.message(Command("cancel"))
async def cancel_any(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await state.clear()
    await message.answer("❌ Cancelled.", reply_markup=admin_keyboard())


# ============================================================
# BLOCK CHECK FOR NORMAL MESSAGES
# ============================================================
@dp.message()
async def fallback(message: Message):
    add_user(message)
    if is_blocked(message.from_user.id):
        return
    # Keep the bot simple: normal users only need /start.
    if message.text:
        await message.answer(
            "Use /start to open the menu."
        )


# ============================================================
# RUN
# ============================================================
async def main():
    logging.basicConfig(level=logging.INFO)
    print("🤖 Bot is running...")
    print(f"👤 Admin ID: {ADMIN_ID}")
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
