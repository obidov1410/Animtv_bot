
import asyncio
import json
import logging
import os
import re
import sqlite3
from datetime import datetime, timezone
from difflib import SequenceMatcher
from typing import Optional

from aiogram import Bot, Dispatcher, F, Router, html
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode, ChatMemberStatus
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiohttp import web

# ============================================================
# CONFIG
# ============================================================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
OWNER_ID = int(os.getenv("OWNER_ID", "0"))
DB_PATH = os.getenv("DB_PATH", "anime_bot.db")
PORT = int(os.getenv("PORT", "10000"))

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN environment variable is not set.")
if not OWNER_ID:
    raise RuntimeError("OWNER_ID environment variable is not set.")

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("anime-bot")

db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.row_factory = sqlite3.Row
db.execute("PRAGMA journal_mode=WAL")
db.execute("PRAGMA foreign_keys=ON")

# ============================================================
# DATABASE
# ============================================================
def init_db():
    db.executescript("""
    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        first_name TEXT,
        joined_at TEXT NOT NULL,
        is_active INTEGER NOT NULL DEFAULT 1
    );

    CREATE TABLE IF NOT EXISTS admins (
        user_id INTEGER PRIMARY KEY,
        role TEXT NOT NULL DEFAULT 'admin',
        permissions TEXT NOT NULL DEFAULT '{}',
        added_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS required_channels (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id INTEGER NOT NULL UNIQUE,
        title TEXT,
        invite_url TEXT,
        added_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS animes (
        code INTEGER PRIMARY KEY,
        name TEXT,
        source_chat_id INTEGER NOT NULL,
        source_message_id INTEGER NOT NULL,
        description TEXT DEFAULT '',
        created_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS anime_aliases (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        code INTEGER NOT NULL,
        alias TEXT NOT NULL,
        UNIQUE(code, alias),
        FOREIGN KEY(code) REFERENCES animes(code) ON DELETE CASCADE
    );
    """)
    defaults = {
        "start_text": (
            "👋 <b>Anime botga xush kelibsiz!</b>\\n\\n"
            "🔢 Anime kodini yuboring yoki nomini yozing.\\n"
            "🔎 Nomini eslay olmasangiz, <b>Anime topish</b> tugmasidan foydalaning."
        ),
        "bot_paused": "0",
        "button1_text": "🔎 Anime topish",
        "button1_callback": "find_anime",
        "button2_text": "📖 Yordam",
        "button2_callback": "help",
    }
    for k, v in defaults.items():
        db.execute("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)", (k, v))
    db.commit()

def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

def get_setting(key, default=""):
    row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default

def set_setting(key, value):
    db.execute("INSERT INTO settings(key,value) VALUES(?,?) "
               "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))
    db.commit()

def user_count():
    return db.execute("SELECT COUNT(*) c FROM users WHERE is_active=1").fetchone()["c"]

def upsert_user(message: Message):
    u = message.from_user
    exists = db.execute("SELECT 1 FROM users WHERE user_id=?", (u.id,)).fetchone()
    if not exists:
        db.execute(
            "INSERT INTO users(user_id,username,first_name,joined_at) VALUES(?,?,?,?)",
            (u.id, u.username, u.first_name, now()),
        )
        db.commit()
        return True
    db.execute(
        "UPDATE users SET username=?, first_name=?, is_active=1 WHERE user_id=?",
        (u.username, u.first_name, u.id),
    )
    db.commit()
    return False

def is_owner(user_id):
    return user_id == OWNER_ID

def admin_row(user_id):
    return db.execute("SELECT * FROM admins WHERE user_id=?", (user_id,)).fetchone()

def is_admin(user_id):
    return is_owner(user_id) or admin_row(user_id) is not None

def has_perm(user_id, permission):
    if is_owner(user_id):
        return True
    row = admin_row(user_id)
    if not row:
        return False
    try:
        perms = json.loads(row["permissions"])
    except Exception:
        perms = {}
    return bool(perms.get(permission, False))

def add_admin(user_id, permissions=None):
    perms = permissions or {
        "anime": True, "users": True, "channels": True,
        "settings": True, "admins": False
    }
    db.execute(
        "INSERT INTO admins(user_id,role,permissions,added_at) VALUES(?,?,?,?) "
        "ON CONFLICT(user_id) DO UPDATE SET permissions=excluded.permissions",
        (user_id, "admin", json.dumps(perms), now())
    )
    db.commit()

def remove_admin(user_id):
    db.execute("DELETE FROM admins WHERE user_id=?", (user_id,))
    db.commit()

def next_code():
    row = db.execute("SELECT COALESCE(MAX(code),0)+1 AS n FROM animes").fetchone()
    return row["n"]

def get_anime(code):
    return db.execute("SELECT * FROM animes WHERE code=?", (code,)).fetchone()

def search_animes(query, limit=10):
    q = query.strip().lower()
    rows = db.execute("SELECT * FROM animes ORDER BY code DESC").fetchall()
    scored = []
    for r in rows:
        names = [r["name"] or ""]
        aliases = db.execute(
            "SELECT alias FROM anime_aliases WHERE code=?", (r["code"],)
        ).fetchall()
        names += [a["alias"] for a in aliases]
        best = max(SequenceMatcher(None, q, n.lower()).ratio() for n in names if n)
        if q in (r["name"] or "").lower():
            best += 0.35
        scored.append((best, r))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [r for score, r in scored[:limit] if score >= 0.28]

def tokenize(s):
    return set(re.findall(r"[a-zA-Zа-яА-ЯёЁўқғҳЎҚҒҲ0-9]+", s.lower()))

# ============================================================
# KEYBOARDS
# ============================================================
def start_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=get_setting("button1_text", "🔎 Anime topish"),
            callback_data=get_setting("button1_callback", "find_anime")
        )],
        [InlineKeyboardButton(
            text=get_setting("button2_text", "📖 Yordam"),
            callback_data=get_setting("button2_callback", "help")
        )],
    ])

def confirm_kb(prefix):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Tasdiqlash", callback_data=f"{prefix}:yes"),
        InlineKeyboardButton(text="❌ Bekor qilish", callback_data=f"{prefix}:no"),
    ]])

def admin_kb(user_id):
    rows = [
        [InlineKeyboardButton(text="➕ Anime qo‘shish", callback_data="adm:addanime")],
        [InlineKeyboardButton(text="👥 Foydalanuvchilar", callback_data="adm:users")],
        [InlineKeyboardButton(text="📢 Majburiy obuna", callback_data="adm:channels")],
        [InlineKeyboardButton(text="⚙️ Sozlamalar", callback_data="adm:settings")],
    ]
    if is_owner(user_id):
        rows.append([InlineKeyboardButton(text="👑 Adminlar", callback_data="adm:admins")])
        rows.append([InlineKeyboardButton(text="⏸ Bot holati", callback_data="adm:pause")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

# ============================================================
# FSM
# ============================================================
class AddAnime(StatesGroup):
    waiting_source = State()
    waiting_name = State()
    waiting_description = State()

class AddAdmin(StatesGroup):
    waiting_user_id = State()

class RemoveAdmin(StatesGroup):
    waiting_user_id = State()

class AddChannel(StatesGroup):
    waiting_chat_id = State()
    waiting_invite = State()

class SetText(StatesGroup):
    waiting_text = State()

class AnimeFinder(StatesGroup):
    clue = State()
    q1 = State()
    q2 = State()
    q3 = State()

# ============================================================
# ROUTER
# ============================================================
router = Router()

async def notify_new_user(bot: Bot, message: Message):
    text = (
        "🆕 <b>Yangi foydalanuvchi!</b>\\n\\n"
        f"👤 Username: {('@' + message.from_user.username) if message.from_user.username else 'yo‘q'}\\n"
        f"🆔 ID: <code>{message.from_user.id}</code>\\n"
        f"📊 Umumiy: <b>{user_count()}</b>\\n"
        f"📅 Qo‘shilgan: <code>{now()}</code>"
    )
    admin_ids = [OWNER_ID] + [r["user_id"] for r in db.execute("SELECT user_id FROM admins")]
    for aid in set(admin_ids):
        try:
            await bot.send_message(aid, text)
        except Exception:
            pass

async def required_channels_ok(bot: Bot, user_id: int):
    rows = db.execute("SELECT * FROM required_channels").fetchall()
    missing = []
    for ch in rows:
        try:
            member = await bot.get_chat_member(ch["chat_id"], user_id)
            if member.status in {ChatMemberStatus.LEFT, ChatMemberStatus.KICKED}:
                missing.append(ch)
        except Exception:
            missing.append(ch)
    return missing

def subscription_kb(channels):
    rows = []
    for ch in channels:
        if ch["invite_url"]:
            rows.append([InlineKeyboardButton(text=f"📢 {ch['title'] or 'Kanal'}", url=ch["invite_url"])])
    rows.append([InlineKeyboardButton(text="✅ Tekshirish", callback_data="check_sub")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

async def gate(message: Message, bot: Bot):
    if is_admin(message.from_user.id):
        return True
    missing = await required_channels_ok(bot, message.from_user.id)
    if missing:
        await message.answer(
            "🔒 Botdan foydalanish uchun quyidagi kanallarga obuna bo‘ling:",
            reply_markup=subscription_kb(missing)
        )
        return False
    return True

# ============================================================
# START / SEARCH
# ============================================================
@router.message(CommandStart())
async def start(message: Message, state: FSMContext, bot: Bot):
    new = upsert_user(message)
    if new:
        await notify_new_user(bot, message)

    if get_setting("bot_paused") == "1" and not is_admin(message.from_user.id):
        await message.answer("⏸ Bot vaqtincha texnik ishlar sababli to‘xtatilgan.")
        return

    if not await gate(message, bot):
        return

    await state.clear()
    await message.answer(get_setting("start_text"), reply_markup=start_kb())

@router.callback_query(F.data == "check_sub")
async def check_sub(call: CallbackQuery, bot: Bot):
    missing = await required_channels_ok(bot, call.from_user.id)
    if missing:
        await call.answer("Hali barcha kanallarga obuna bo‘lmagansiz.", show_alert=True)
    else:
        await call.answer("Obuna tasdiqlandi!")
        await call.message.edit_text(get_setting("start_text"), reply_markup=start_kb())

@router.callback_query(F.data == "help")
async def help_cb(call: CallbackQuery):
    await call.answer()
    await call.message.answer(
        "📖 <b>Foydalanish:</b>\\n"
        "• Anime kodi: <code>124</code>\\n"
        "• Anime nomi: <code>Black Clover</code>\\n"
        "• Eslagan voqeangizni yozing — topishga yordam beraman."
    )

@router.message(F.text.regexp(r"^\d+$"))
async def code_search(message: Message, bot: Bot):
    upsert_user(message)
    if not await gate(message, bot):
        return
    anime = get_anime(int(message.text))
    if not anime:
        await message.answer("❌ Bunday anime kodi topilmadi.")
        return
    await send_anime(bot, message.chat.id, anime)

async def send_anime(bot: Bot, chat_id: int, anime):
    caption = f"🎬 <b>{html.quote(anime['name'] or 'Nomsiz anime')}</b>\\n🔢 Kod: <code>{anime['code']}</code>"
    if anime["description"]:
        caption += f"\\n\\n{html.quote(anime['description'])}"
    try:
        await bot.copy_message(
            chat_id=chat_id,
            from_chat_id=anime["source_chat_id"],
            message_id=anime["source_message_id"]
        )
        await bot.send_message(chat_id, caption)
    except TelegramBadRequest as e:
        await bot.send_message(
            chat_id,
            "⚠️ Anime manbasidagi postni yuborib bo‘lmadi. "
            "Bot kanalga kirish huquqiga ega ekanini tekshiring."
        )
        log.warning("copy_message failed: %s", e)

@router.message(F.text)
async def text_search(message: Message, bot: Bot):
    if message.text.startswith("/"):
        return
    upsert_user(message)
    if not await gate(message, bot):
        return
    results = search_animes(message.text)
    if not results:
        await message.answer(
            "🤔 Aniq topilmadi.\n\n"
            "Agar anime voqeasini eslasangiz, masalan:\n"
            "“bosh qahramon boshqa dunyoga tushadi...” deb yozing."
        )
        return
    rows = []
    for r in results[:8]:
        rows.append([InlineKeyboardButton(
            text=f"🎬 {r['name'] or 'Nomsiz'} [{r['code']}]",
            callback_data=f"anime:{r['code']}"
        )])
    await message.answer("🔎 O‘xshash animelar:", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))

@router.callback_query(F.data.startswith("anime:"))
async def anime_callback(call: CallbackQuery, bot: Bot):
    await call.answer()
    code = int(call.data.split(":")[1])
    anime = get_anime(code)
    if anime:
        await send_anime(bot, call.message.chat.id, anime)

# ============================================================
# "ESLAY OLMAYMAN" FINDER
# ============================================================
@router.callback_query(F.data == "find_anime")
async def finder_start(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await state.set_state(AnimeFinder.clue)
    await call.message.answer(
        "🔎 Anime topamiz.\n\n"
        "Eslagan voqeangizni batafsil yozing. "
        "Masalan: qahramon kim, qaysi dunyo, qanday kuchlar, nima bo‘lgan?"
    )

@router.message(AnimeFinder.clue)
async def finder_clue(message: Message, state: FSMContext):
    await state.update_data(clue=message.text)
    await state.set_state(AnimeFinder.q1)
    await message.answer("1️⃣ Qahramonning jinsi yoki taxminiy yoshini eslaysizmi?")

@router.message(AnimeFinder.q1)
async def finder_q1(message: Message, state: FSMContext):
    await state.update_data(q1=message.text)
    await state.set_state(AnimeFinder.q2)
    await message.answer("2️⃣ Anime qaysi janrga yaqin: jangari, fantastika, komediya, romantika yoki boshqami?")

@router.message(AnimeFinder.q2)
async def finder_q2(message: Message, state: FSMContext):
    await state.update_data(q2=message.text)
    await state.set_state(AnimeFinder.q3)
    await message.answer("3️⃣ Yana bir eslagan detalni yozing: joy, qahramon, kuch, qurol yoki voqea.")

@router.message(AnimeFinder.q3)
async def finder_q3(message: Message, state: FSMContext):
    data = await state.get_data()
    clue = " ".join([data.get("clue",""), data.get("q1",""), data.get("q2",""), message.text])
    clue_tokens = tokenize(clue)

    rows = db.execute("SELECT * FROM animes").fetchall()
    scored = []
    for r in rows:
        text = (r["name"] or "") + " " + (r["description"] or "")
        aliases = db.execute("SELECT alias FROM anime_aliases WHERE code=?", (r["code"],)).fetchall()
        text += " " + " ".join(a["alias"] for a in aliases)
        t = tokenize(text)
        overlap = len(clue_tokens & t)
        ratio = SequenceMatcher(None, clue.lower(), text.lower()).ratio()
        score = overlap * 0.18 + ratio
        scored.append((score, r))
    scored.sort(key=lambda x: x[0], reverse=True)
    await state.clear()

    if not scored:
        await message.answer("❌ Hozircha bazada anime yo‘q.")
        return

    best = scored[:5]
    rows_kb = [[InlineKeyboardButton(
        text=f"🎬 {r['name'] or 'Nomsiz'} [{r['code']}]",
        callback_data=f"anime:{r['code']}"
    )] for _, r in best]
    await message.answer(
        "🧠 Eslagan ma’lumotlaringiz bo‘yicha eng yaqin variantlar:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows_kb)
    )

# ============================================================
# ADMIN
# ============================================================
@router.message(Command("admin"))
async def admin_command(message: Message):
    if not is_admin(message.from_user.id):
        return
    await message.answer("👑 <b>Admin panel</b>", reply_markup=admin_kb(message.from_user.id))

@router.callback_query(F.data == "adm:addanime")
async def adm_addanime(call: CallbackQuery, state: FSMContext):
    if not has_perm(call.from_user.id, "anime"):
        await call.answer("Vakolat yo‘q.", show_alert=True); return
    await call.answer()
    await state.set_state(AddAnime.waiting_source)
    await call.message.answer(
        "➕ Anime qo‘shish\\n\\n"
        "Endi <b>kanaldagi anime postini forward</b> qilib shu yerga yuboring.\\n"
        "Bot avtomatik keyingi kodni beradi va tasdiqlashni so‘raydi."
    )

@router.message(AddAnime.waiting_source)
async def addanime_source(message: Message, state: FSMContext):
    if not has_perm(message.from_user.id, "anime"):
        return
    origin = getattr(message, "forward_origin", None)
    if not origin or not hasattr(origin, "chat") or not hasattr(origin, "message_id"):
        await message.answer("❌ Iltimos, kanal postini Forward qilib yuboring.")
        return
    code = next_code()
    await state.update_data(
        code=code,
        source_chat_id=origin.chat.id,
        source_message_id=origin.message_id
    )
    await state.set_state(AddAnime.waiting_name)
    await message.answer(
        f"🔢 Yangi anime kodi: <code>{code}</code>\\n\\n"
        "Anime nomini yozing:"
    )

@router.message(AddAnime.waiting_name)
async def addanime_name(message: Message, state: FSMContext):
    data = await state.get_data()
    await state.update_data(name=message.text.strip())
    await state.set_state(AddAnime.waiting_description)
    await message.answer("📝 Qisqa tavsif yozing yoki <code>-</code> yuboring.")

@router.message(AddAnime.waiting_description)
async def addanime_desc(message: Message, state: FSMContext):
    data = await state.get_data()
    description = "" if message.text.strip() == "-" else message.text.strip()
    await state.update_data(description=description)
    data = await state.get_data()
    await message.answer(
        "Tasdiqlaysizmi?\\n\\n"
        f"🔢 Kod: <code>{data['code']}</code>\\n"
        f"🎬 Nomi: <b>{html.quote(data['name'])}</b>\\n"
        f"📝 Tavsif: {html.quote(description) if description else 'yo‘q'}",
        reply_markup=confirm_kb("addanime")
    )

@router.callback_query(F.data == "addanime:yes")
async def addanime_confirm(call: CallbackQuery, state: FSMContext):
    if not has_perm(call.from_user.id, "anime"):
        await call.answer("Vakolat yo‘q.", show_alert=True); return
    data = await state.get_data()
    db.execute(
        "INSERT INTO animes(code,name,source_chat_id,source_message_id,description,created_at) "
        "VALUES(?,?,?,?,?,?)",
        (data["code"], data["name"], data["source_chat_id"], data["source_message_id"],
         data["description"], now())
    )
    db.commit()
    await state.clear()
    await call.answer("Saqlandi!")
    await call.message.edit_text(
        f"✅ Anime qo‘shildi!\\n\\n"
        f"🎬 {html.quote(data['name'])}\\n"
        f"🔢 Kod: <code>{data['code']}</code>"
    )

@router.callback_query(F.data == "addanime:no")
async def addanime_cancel(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.answer("Bekor qilindi.")
    await call.message.edit_text("❌ Anime qo‘shish bekor qilindi.")

# User requested /addname/123/Name format. It is parsed as normal text.
@router.message(F.text.regexp(r"^/addname/\d+/.+/$"))
async def add_name_command(message: Message):
    if not has_perm(message.from_user.id, "anime"):
        return
    m = re.match(r"^/addname/(\d+)/(.+)/$", message.text.strip(), re.S)
    if not m:
        return
    code, name = int(m.group(1)), m.group(2).strip()
    anime = get_anime(code)
    if not anime:
        await message.answer("❌ Bunday kodli anime yo‘q.")
        return
    await message.answer(
        f"🔢 Kod: <code>{code}</code>\\n"
        f"🎬 Yangi nom: <b>{html.quote(name)}</b>\\n\\nTasdiqlaysizmi?",
        reply_markup=confirm_kb(f"addname:{code}")
    )
    # Store pending name in settings under a per-user key.
    set_setting(f"pending_name_{message.from_user.id}", json.dumps({"code": code, "name": name}))

@router.callback_query(F.data.startswith("addname:") & F.data.endswith(":yes"))
async def add_name_confirm(call: CallbackQuery):
    # callback format addname:123:yes
    if not has_perm(call.from_user.id, "anime"):
        return
    parts = call.data.split(":")
    code = int(parts[1])
    payload = get_setting(f"pending_name_{call.from_user.id}", "")
    if not payload:
        await call.answer("Tasdiqlash ma'lumoti topilmadi.", show_alert=True); return
    obj = json.loads(payload)
    db.execute("UPDATE animes SET name=? WHERE code=?", (obj["name"], code))
    db.commit()
    set_setting(f"pending_name_{call.from_user.id}", "")
    await call.answer("Nom saqlandi!")
    await call.message.edit_text(f"✅ <code>{code}</code> kodi nomi yangilandi: <b>{html.quote(obj['name'])}</b>")

# ============================================================
# OWNER / ADMIN MANAGEMENT
# ============================================================
@router.callback_query(F.data == "adm:users")
async def adm_users(call: CallbackQuery):
    if not has_perm(call.from_user.id, "users"):
        await call.answer("Vakolat yo‘q.", show_alert=True); return
    await call.answer()
    count = user_count()
    await call.message.answer(f"👥 Faol foydalanuvchilar: <b>{count}</b>")

@router.callback_query(F.data == "adm:admins")
async def adm_admins(call: CallbackQuery):
    if not is_owner(call.from_user.id):
        await call.answer("Faqat asosiy admin.", show_alert=True); return
    rows = db.execute("SELECT * FROM admins ORDER BY added_at").fetchall()
    text = "👑 <b>Adminlar</b>\\n\\n"
    if not rows:
        text += "Hozircha qo‘shimcha admin yo‘q."
    else:
        text += "\\n".join(f"• <code>{r['user_id']}</code> — {r['role']}" for r in rows)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Admin qo‘shish", callback_data="adm:addadmin")],
        [InlineKeyboardButton(text="➖ Admin o‘chirish", callback_data="adm:deladmin")],
    ])
    await call.message.answer(text, reply_markup=kb)

@router.callback_query(F.data == "adm:addadmin")
async def addadmin_start(call: CallbackQuery, state: FSMContext):
    if not is_owner(call.from_user.id):
        await call.answer("Faqat asosiy admin.", show_alert=True); return
    await call.answer()
    await state.set_state(AddAdmin.waiting_user_id)
    await call.message.answer("Yangi adminning Telegram ID raqamini yuboring.")

@router.message(AddAdmin.waiting_user_id)
async def addadmin_finish(message: Message, state: FSMContext):
    if not is_owner(message.from_user.id):
        return
    try:
        uid = int(message.text.strip())
    except ValueError:
        await message.answer("❌ ID faqat raqam bo‘lsin.")
        return
    add_admin(uid)
    await state.clear()
    await message.answer(f"✅ <code>{uid}</code> admin qilindi.")

@router.callback_query(F.data == "adm:deladmin")
async def deladmin_start(call: CallbackQuery, state: FSMContext):
    if not is_owner(call.from_user.id):
        await call.answer("Faqat asosiy admin.", show_alert=True)
        return
    await call.answer()
    await state.set_state(RemoveAdmin.waiting_user_id)
    await call.message.answer("O‘chirmoqchi bo‘lgan admin ID'sini yuboring.")

@router.message(RemoveAdmin.waiting_user_id)
async def deladmin_finish(message: Message, state: FSMContext):
    if not is_owner(message.from_user.id):
        return
    try:
        uid = int(message.text.strip())
    except ValueError:
        await message.answer("❌ ID faqat raqam bo‘lsin.")
        return
    if uid == OWNER_ID:
        await message.answer("❌ Asosiy adminni o‘chirib bo‘lmaydi.")
        await state.clear()
        return
    if not admin_row(uid):
        await message.answer("❌ Bu ID admin emas.")
        await state.clear()
        return
    remove_admin(uid)
    await state.clear()
    await message.answer(f"✅ <code>{uid}</code> adminlardan o‘chirildi.")

# ============================================================
# FORCED SUBSCRIPTIONS
# ============================================================
@router.callback_query(F.data == "adm:channels")
async def adm_channels(call: CallbackQuery):
    if not has_perm(call.from_user.id, "channels"):
        await call.answer("Vakolat yo‘q.", show_alert=True); return
    rows = db.execute("SELECT * FROM required_channels").fetchall()
    text = "📢 <b>Majburiy obuna kanallari</b>\\n\\n"
    text += "\\n".join(f"• {r['title'] or r['chat_id']} — <code>{r['chat_id']}</code>" for r in rows) or "Yo‘q."
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Kanal qo‘shish", callback_data="channel:add")],
        [InlineKeyboardButton(text="➖ Kanal o‘chirish", callback_data="channel:del")],
    ])
    await call.message.answer(text, reply_markup=kb)

@router.callback_query(F.data == "channel:add")
async def channel_add_start(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await state.set_state(AddChannel.waiting_chat_id)
    await call.message.answer("Kanal ID sini yuboring. Masalan: <code>-1001234567890</code>")

@router.message(AddChannel.waiting_chat_id)
async def channel_add_id(message: Message, state: FSMContext, bot: Bot):
    try:
        chat_id = int(message.text.strip())
    except ValueError:
        await message.answer("❌ Kanal ID noto‘g‘ri.")
        return
    try:
        chat = await bot.get_chat(chat_id)
    except Exception:
        await message.answer("❌ Bot kanalni topa olmadi. Bot kanalga admin qilib qo‘yilganini tekshiring.")
        return
    await state.update_data(chat_id=chat_id, title=chat.title or str(chat_id))
    await state.set_state(AddChannel.waiting_invite)
    await message.answer("Kanalga kirish linkini yuboring (masalan https://t.me/...).")

@router.message(AddChannel.waiting_invite)
async def channel_add_finish(message: Message, state: FSMContext):
    data = await state.get_data()
    db.execute(
        "INSERT OR REPLACE INTO required_channels(chat_id,title,invite_url,added_at) VALUES(?,?,?,?)",
        (data["chat_id"], data["title"], message.text.strip(), now())
    )
    db.commit()
    await state.clear()
    await message.answer("✅ Majburiy obuna kanali qo‘shildi.")

# ============================================================
# SETTINGS
# ============================================================
@router.callback_query(F.data == "adm:settings")
async def adm_settings(call: CallbackQuery):
    if not has_perm(call.from_user.id, "settings"):
        await call.answer("Vakolat yo‘q.", show_alert=True); return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Start xabarini o‘zgartirish", callback_data="set:start")],
        [InlineKeyboardButton(text="🔘 1-tugmani o‘zgartirish", callback_data="set:b1")],
        [InlineKeyboardButton(text="🔘 2-tugmani o‘zgartirish", callback_data="set:b2")],
    ])
    await call.message.answer("⚙️ Sozlamalar", reply_markup=kb)

@router.callback_query(F.data.in_({"set:start", "set:b1", "set:b2"}))
async def setting_start(call: CallbackQuery, state: FSMContext):
    if not has_perm(call.from_user.id, "settings"):
        await call.answer("Vakolat yo‘q.", show_alert=True); return
    await call.answer()
    await state.update_data(setting=call.data)
    await state.set_state(SetText.waiting_text)
    await call.message.answer("Yangi matnni yuboring.")

@router.message(SetText.waiting_text)
async def setting_finish(message: Message, state: FSMContext):
    data = await state.get_data()
    setting = data["setting"]
    if setting == "set:start":
        set_setting("start_text", message.text)
    elif setting == "set:b1":
        set_setting("button1_text", message.text)
    elif setting == "set:b2":
        set_setting("button2_text", message.text)
    await state.clear()
    await message.answer("✅ Saqlandi.")

@router.callback_query(F.data == "adm:pause")
async def adm_pause(call: CallbackQuery):
    if not is_owner(call.from_user.id):
        await call.answer("Faqat asosiy admin.", show_alert=True); return
    new = "0" if get_setting("bot_paused") == "1" else "1"
    set_setting("bot_paused", new)
    await call.answer("Holat o‘zgartirildi.")
    await call.message.answer("▶️ Bot yoqildi." if new == "0" else "⏸ Bot vaqtincha to‘xtatildi.")

# ============================================================
# ADMIN COMMANDS
# ============================================================
@router.message(Command("stats"))
async def stats(message: Message):
    if not is_admin(message.from_user.id):
        return
    anime_count = db.execute("SELECT COUNT(*) c FROM animes").fetchone()["c"]
    admin_count = db.execute("SELECT COUNT(*) c FROM admins").fetchone()["c"]
    channel_count = db.execute("SELECT COUNT(*) c FROM required_channels").fetchone()["c"]
    await message.answer(
        f"📊 <b>Statistika</b>\\n\\n"
        f"👥 Users: {user_count()}\\n"
        f"🎬 Animelar: {anime_count}\\n"
        f"👑 Adminlar: {admin_count + 1}\\n"
        f"📢 Majburiy kanallar: {channel_count}"
    )

@router.message(Command("cancel"))
async def cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("❌ Amal bekor qilindi.")

# ============================================================
# HEALTH / UPTIME SERVER
# ============================================================
async def health(request: web.Request):
    return web.json_response({
        "status": "ok",
        "service": "anime-bot"
    })

async def root(request: web.Request):
    return web.Response(text="Anime bot is running.")

async def start_web_server():
    app = web.Application()
    app.router.add_get("/", root)
    app.router.add_get("/health", health)

    runner = web.AppRunner(app)
    await runner.setup()

    site = web.TCPSite(runner, host="0.0.0.0", port=PORT)
    await site.start()

    log.info("HTTP server started on 0.0.0.0:%s", PORT)
    return runner

async def run_bot(bot: Bot, dp: Dispatcher):
    await bot.delete_webhook(drop_pending_updates=True)
    log.info("Telegram polling started.")
    await dp.start_polling(bot)

# ============================================================
# MAIN
# ============================================================
async def main():
    init_db()

    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML)
    )

    dp = Dispatcher()
    dp.include_router(router)

    # Render web service + Telegram polling run together.
    web_runner = await start_web_server()

    try:
        await run_bot(bot, dp)
    finally:
        await web_runner.cleanup()
        await bot.session.close()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        log.info("Anime bot stopped.")
