import os
import time
import asyncio
import logging
import aiosqlite
import aiohttp
from aiohttp import web

from aiogram import Bot, Dispatcher, F, Router
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    LabeledPrice, PreCheckoutQuery
)
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest

# ==================== CONFIGURATION ====================
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
SMM_API_KEY = os.getenv("SMM_API_KEY", "")
DATABASE_URL = os.getenv("DATABASE_URL", "")
SMM_API_URL = "https://kometa-smm.uz/api/v2"

ADMIN_ID = 5523761749
MARKUP = 1.70  # +70% Reseller Markup
UZS_PER_STAR = int(os.getenv("UZS_PER_STAR", "250"))
PORT = int(os.getenv("PORT", "8080"))

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

USE_POSTGRES = bool(DATABASE_URL)

# Emojis
E_GREEN = "🟢"
E_RED = "🔴"
E_YELLOW = "🟡"
E_BLUE = "🔵"
E_FIRE = "🔥"
E_STAR = "⭐️"
E_DIAMOND = "💎"
E_ROCKET = "🚀"
E_BAG = "💰"
E_CART = "🛍"
E_CHECK = "✅"
E_CROSS = "❌"
E_CLOCK = "⏳"
E_BACK = "🔙"
E_BOX = "📦"
E_REFRESH = "🔄"
E_LINK = "🔗"
E_HASH = "🔢"

services_cache = {
    "data": [],
    "platforms": {"Instagram": [], "Telegram": [], "YouTube": [], "TikTok": [], "Boshqa": []},
    "by_id": {},
    "last_updated": 0
}
CACHE_TTL = 600


async def init_db():
    if USE_POSTGRES:
        import asyncpg
        conn = await asyncpg.connect(DATABASE_URL)
        try:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id BIGINT PRIMARY KEY,
                    balance DOUBLE PRECISION DEFAULT 0.0,
                    total_spent DOUBLE PRECISION DEFAULT 0.0
                )
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS orders (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT,
                    api_order_id BIGINT,
                    service_id INTEGER,
                    service_name TEXT,
                    link TEXT,
                    quantity INTEGER,
                    charge DOUBLE PRECISION,
                    status TEXT DEFAULT 'Pending',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
        finally:
            await conn.close()
    else:
        async with aiosqlite.connect("reseller_smm.db") as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    balance REAL DEFAULT 0.0,
                    total_spent REAL DEFAULT 0.0
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    api_order_id INTEGER,
                    service_id INTEGER,
                    service_name TEXT,
                    link TEXT,
                    quantity INTEGER,
                    charge REAL,
                    status TEXT DEFAULT 'Pending',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            await db.commit()


async def get_user_balance(user_id: int) -> float:
    if USE_POSTGRES:
        import asyncpg
        conn = await asyncpg.connect(DATABASE_URL)
        try:
            row = await conn.fetchrow("SELECT balance FROM users WHERE user_id = $1", user_id)
            if not row:
                await conn.execute("INSERT INTO users (user_id, balance) VALUES ($1, 0.0) ON CONFLICT (user_id) DO NOTHING", user_id)
                return 0.0
            return round(float(row["balance"]), 2)
        finally:
            await conn.close()
    else:
        async with aiosqlite.connect("reseller_smm.db") as db:
            async with db.execute("SELECT balance FROM users WHERE user_id = ?", (user_id,)) as cur:
                row = await cur.fetchone()
                if not row:
                    await db.execute("INSERT OR IGNORE INTO users (user_id, balance) VALUES (?, 0.0)", (user_id,))
                    await db.commit()
                    return 0.0
                return round(float(row[0]), 2)


async def update_user_balance(user_id: int, delta: float):
    await get_user_balance(user_id)
    if USE_POSTGRES:
        import asyncpg
        conn = await asyncpg.connect(DATABASE_URL)
        try:
            if delta < 0:
                await conn.execute(
                    "UPDATE users SET balance = balance + $1, total_spent = total_spent + $2 WHERE user_id = $3",
                    delta, abs(delta), user_id
                )
            else:
                await conn.execute("UPDATE users SET balance = balance + $1 WHERE user_id = $2", delta, user_id)
        finally:
            await conn.close()
    else:
        async with aiosqlite.connect("reseller_smm.db") as db:
            if delta < 0:
                await db.execute(
                    "UPDATE users SET balance = balance + ?, total_spent = total_spent + ? WHERE user_id = ?",
                    (delta, abs(delta), user_id)
                )
            else:
                await db.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (delta, user_id))
            await db.commit()


async def smm_request(payload: dict):
    payload["key"] = SMM_API_KEY
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        try:
            async with session.post(SMM_API_URL, data=payload) as resp:
                return await resp.json(content_type=None)
        except Exception as e:
            logger.error(f"SMM API Error: {e}")
            return {"error": f"Ulanishda xatolik: {str(e)}"}


async def get_services(force=False):
    now = time.time()
    if not force and services_cache["data"] and (now - services_cache["last_updated"] < CACHE_TTL):
        return services_cache

    res = await smm_request({"action": "services"})
    if isinstance(res, list):
        platforms = {"Instagram": [], "Telegram": [], "YouTube": [], "TikTok": [], "Boshqa": []}
        by_id = {}
        for s in res:
            orig_rate = float(s.get("rate", 0))
            resell_rate = round(orig_rate * MARKUP, 2)
            s["reseller_rate"] = resell_rate

            sid = int(s["service"])
            cat = s.get("category", "").lower()
            by_id[sid] = s

            if "instagram" in cat:
                platforms["Instagram"].append(s)
            elif "telegram" in cat or "tg" in cat:
                platforms["Telegram"].append(s)
            elif "youtube" in cat or "yt" in cat:
                platforms["YouTube"].append(s)
            elif "tiktok" in cat or "tt" in cat:
                platforms["TikTok"].append(s)
            else:
                platforms["Boshqa"].append(s)

        services_cache["data"] = res
        services_cache["platforms"] = platforms
        services_cache["by_id"] = by_id
        services_cache["last_updated"] = now

    return services_cache


class OrderFlow(StatesGroup):
    waiting_link = State()
    waiting_quantity = State()


class TopUpFlow(StatesGroup):
    waiting_custom_stars = State()


class BroadcastFlow(StatesGroup):
    waiting_message = State()


def main_menu_kb(is_admin: bool = False) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=f"{E_GREEN} 🛍 Xizmatlar Katalogi", callback_data="catalog:main")],
        [
            InlineKeyboardButton(text=f"{E_BLUE} 💰 Hisobim", callback_data="menu:balance"),
            InlineKeyboardButton(text=f"{E_YELLOW} ⭐️ Stars To'ldirish", callback_data="menu:topup")
        ],
        [
            InlineKeyboardButton(text=f"{E_BLUE} 📦 Buyurtmalarim", callback_data="orders:0"),
            InlineKeyboardButton(text=f"{E_GREEN} 💎 Qoidalar", callback_data="menu:help")
        ]
    ]
    if is_admin:
        rows.append([InlineKeyboardButton(text=f"{E_RED} ⚡️ Admin Panel", callback_data="admin:panel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def back_home_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"{E_BACK} 🔙 Asosiy menyu", callback_data="menu:home")]
    ])


router = Router()


async def safe_edit(call: CallbackQuery, text: str, reply_markup: InlineKeyboardMarkup = None):
    try:
        await call.message.edit_text(text=text, reply_markup=reply_markup)
    except TelegramBadRequest as e:
        if "message is not modified" in str(e):
            await call.answer()
        else:
            await call.message.answer(text=text, reply_markup=reply_markup)


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    bal = await get_user_balance(message.from_user.id)
    is_admin = (message.from_user.id == ADMIN_ID)
    text = (
        f"{E_FIRE} <b>Assalomu alaykum, {message.from_user.full_name}!</b>\n\n"
        f"{E_BAG} <b>Balansingiz:</b> <code>{bal:,.2f} UZS</code>\n"
        f"{E_STAR} <b>To'lov:</b> Telegram Stars (24/7 Avto)"
    )
    await message.answer(text, reply_markup=main_menu_kb(is_admin))


@router.callback_query(F.data == "menu:home")
async def cb_home(call: CallbackQuery, state: FSMContext):
    await state.clear()
    bal = await get_user_balance(call.from_user.id)
    is_admin = (call.from_user.id == ADMIN_ID)
    text = f"{E_FIRE} <b>Asosiy Menyu</b>\n\n{E_BAG} <b>Balans:</b> <code>{bal:,.2f} UZS</code>"
    await safe_edit(call, text, main_menu_kb(is_admin))


@router.callback_query(F.data == "menu:balance")
async def cb_balance(call: CallbackQuery):
    bal = await get_user_balance(call.from_user.id)
    if USE_POSTGRES:
        import asyncpg
        conn = await asyncpg.connect(DATABASE_URL)
        try:
            row = await conn.fetchrow("SELECT total_spent FROM users WHERE user_id = $1", call.from_user.id)
            spent = float(row["total_spent"]) if row else 0.0
            cnt = await conn.fetchval("SELECT COUNT(*) FROM orders WHERE user_id = $1", call.from_user.id)
        finally:
            await conn.close()
    else:
        async with aiosqlite.connect("reseller_smm.db") as db:
            async with db.execute("SELECT total_spent FROM users WHERE user_id = ?", (call.from_user.id,)) as cur:
                row = await cur.fetchone()
                spent = float(row[0]) if row else 0.0
            async with db.execute("SELECT COUNT(*) FROM orders WHERE user_id = ?", (call.from_user.id,)) as cur:
                cnt = (await cur.fetchone())[0]

    text = f"🆔 ID: <code>{call.from_user.id}</code>\n💰 Balans: <code>{bal:,.2f} UZS</code>\n📦 Buyurtmalar: {cnt} ta"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⭐️ Balansni to'ldirish", callback_data="menu:topup")],
        [InlineKeyboardButton(text="🔙 Orqaga", callback_data="menu:home")]
    ])
    await safe_edit(call, text, kb)


@router.callback_query(F.data == "menu:help")
async def cb_help(call: CallbackQuery):
    await safe_edit(call, "💎 Xizmatdan foydalanish va to'lovlar 24/7 avtomatik ishlaydi.", back_home_kb())


@router.callback_query(F.data == "catalog:main")
async def cb_catalog_main(call: CallbackQuery, state: FSMContext):
    await state.clear()
    cache = await get_services()
    platforms = cache["platforms"]

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text=f"Instagram ({len(platforms['Instagram'])})", callback_data="plat:Instagram:0"),
            InlineKeyboardButton(text=f"Telegram ({len(platforms['Telegram'])})", callback_data="plat:Telegram:0")
        ],
        [
            InlineKeyboardButton(text=f"YouTube ({len(platforms['YouTube'])})", callback_data="plat:YouTube:0"),
            InlineKeyboardButton(text=f"TikTok ({len(platforms['TikTok'])})", callback_data="plat:TikTok:0")
        ],
        [InlineKeyboardButton(text="🔙 Asosiy menyu", callback_data="menu:home")]
    ])
    await safe_edit(call, "🛍 <b>Platformani tanlang:</b>", kb)


@router.callback_query(F.data.startswith("plat:"))
async def cb_platform_services(call: CallbackQuery):
    _, plat_name, page_str = call.data.split(":")
    page = int(page_str)
    cache = await get_services()
    srv_list = cache["platforms"].get(plat_name, [])

    if not srv_list:
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Orqaga", callback_data="catalog:main")]])
        return await safe_edit(call, "Xizmatlar topilmadi.", kb)

    per_page = 6
    total_pages = (len(srv_list) + per_page - 1) // per_page
    start = page * per_page
    current_srvs = srv_list[start:start + per_page]

    rows = []
    for s in current_srvs:
        sid = s["service"]
        name = s["name"]
        rate = s["reseller_rate"]
        rows.append([InlineKeyboardButton(text=f"🟢 #{sid} | {name[:22]} ({rate:,.0f} UZS)", callback_data=f"srv:{sid}:{plat_name}:{page}")])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"plat:{plat_name}:{page-1}"))
    nav.append(InlineKeyboardButton(text=f"📄 {page+1}/{total_pages}", callback_data="noop"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton(text="➡️", callback_data=f"plat:{plat_name}:{page+1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(text="🔙 Orqaga", callback_data="catalog:main")])

    await safe_edit(call, f"📌 <b>{plat_name} xizmatlari:</b>", InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.startswith("srv:"))
async def cb_service_detail(call: CallbackQuery, state: FSMContext):
    await state.clear()
    parts = call.data.split(":")
    sid = int(parts[1])
    plat_name = parts[2]
    page = int(parts[3])

    cache = await get_services()
    s = cache["by_id"].get(sid)
    if not s:
        return await call.answer("Topilmadi", show_alert=True)

    text = f"🚀 <b>#{s['service']} — {s['name']}</b>\n💰 Narxi (1000 ta): <code>{s['reseller_rate']:,.2f} UZS</code>\nMin: {s['min']} | Max: {s['max']}"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Buyurtma berish", callback_data=f"order_start:{sid}")],
        [InlineKeyboardButton(text="🔙 Orqaga", callback_data=f"plat:{plat_name}:{page}")]
    ])
    await safe_edit(call, text, kb)


@router.callback_query(F.data.startswith("order_start:"))
async def cb_order_start(call: CallbackQuery, state: FSMContext):
    sid = int(call.data.split(":")[1])
    await state.set_state(OrderFlow.waiting_link)
    await state.update_data(service_id=sid)
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Bekor qilish", callback_data="catalog:main")]])
    await safe_edit(call, "🔗 Havolani yuboring (Link):", kb)


@router.message(OrderFlow.waiting_link)
async def msg_order_link(message: Message, state: FSMContext):
    link = (message.text or "").strip()
    if not link.startswith("http"):
        return await message.answer("Xato link! https:// bilan boshlansin.")
    await state.update_data(link=link)
    await state.set_state(OrderFlow.waiting_quantity)
    await message.answer("🔢 Miqdorni kiriting (soni):")


@router.message(OrderFlow.waiting_quantity)
async def msg_order_quantity(message: Message, state: FSMContext):
    if not message.text.isdigit():
        return await message.answer("Faqat raqam kiriting!")
    qty = int(message.text)
    data = await state.get_data()
    sid = data["service_id"]
    cache = await get_services()
    s = cache["by_id"].get(sid)

    if qty < int(s["min"]) or qty > int(s["max"]):
        return await message.answer(f"Chegaradan chiqdi! Min: {s['min']}, Max: {s['max']}")

    charge = round((qty / 1000.0) * s["reseller_rate"], 2)
    bal = await get_user_balance(message.from_user.id)
    await state.update_data(quantity=qty, charge=charge)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Tasdiqlash", callback_data="order_confirm")],
        [InlineKeyboardButton(text="❌ Bekor qilish", callback_data="catalog:main")]
    ])
    await message.answer(f"💰 Summa: <code>{charge:,.2f} UZS</code>\nBalansingiz: <code>{bal:,.2f} UZS</code>\nTasdiqlaysizmi?", reply_markup=kb)


@router.callback_query(F.data == "order_confirm")
async def cb_order_confirm(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if not data:
        return await call.answer("Eskirgan ma'lumot", show_alert=True)

    sid, link, qty, charge = data["service_id"], data["link"], data["quantity"], data["charge"]
    await state.clear()

    bal = await get_user_balance(call.from_user.id)
    if bal < charge:
        return await safe_edit(call, "Balansingiz yetarli emas!", back_home_kb())

    await update_user_balance(call.from_user.id, -charge)
    res = await smm_request({"action": "add", "service": sid, "link": link, "quantity": qty})

    if isinstance(res, dict) and "order" in res:
        api_order_id = int(res["order"])
        cache = await get_services()
        s_name = cache["by_id"].get(sid, {}).get("name", "SMM Service")

        if USE_POSTGRES:
            import asyncpg
            conn = await asyncpg.connect(DATABASE_URL)
            try:
                await conn.execute(
                    "INSERT INTO orders (user_id, api_order_id, service_id, service_name, link, quantity, charge, status) VALUES ($1, $2, $3, $4, $5, $6, $7, 'Pending')",
                    call.from_user.id, api_order_id, sid, s_name, link, qty, charge
                )
            finally:
                await conn.close()
        else:
            async with aiosqlite.connect("reseller_smm.db") as db:
                await db.execute(
                    "INSERT INTO orders (user_id, api_order_id, service_id, service_name, link, quantity, charge, status) VALUES (?, ?, ?, ?, ?, ?, ?, 'Pending')",
                    (call.from_user.id, api_order_id, sid, s_name, link, qty, charge)
                )
                await db.commit()

        await safe_edit(call, f"✅ Buyurtma qabul qilindi! ID: #{api_order_id}", back_home_kb())
    else:
        await update_user_balance(call.from_user.id, charge)
        await safe_edit(call, "❌ Xatolik yuz berdi, summa balansingizga qaytarildi.", back_home_kb())


@router.callback_query(F.data.startswith("orders:"))
async def cb_orders_list(call: CallbackQuery):
    page = int(call.data.split(":")[1])
    if USE_POSTGRES:
        import asyncpg
        conn = await asyncpg.connect(DATABASE_URL)
        try:
            rows = await conn.fetch("SELECT api_order_id, service_name, quantity, status FROM orders WHERE user_id = $1 ORDER BY id DESC LIMIT 6 OFFSET $2", call.from_user.id, page * 6)
        finally:
            await conn.close()
    else:
        async with aiosqlite.connect("reseller_smm.db") as db:
            async with db.execute("SELECT api_order_id, service_name, quantity, status FROM orders WHERE user_id = ? ORDER BY id DESC LIMIT 6 OFFSET ?", (call.from_user.id, page * 6)) as cur:
                rows = await cur.fetchall()

    if not rows:
        return await safe_edit(call, "📦 Buyurtmalar yo'q.", back_home_kb())

    rows_kb = []
    for r in rows:
        api_id = r["api_order_id"] if USE_POSTGRES else r[0]
        s_name = r["service_name"] if USE_POSTGRES else r[1]
        qty = r["quantity"] if USE_POSTGRES else r[2]
        status = r["status"] if USE_POSTGRES else r[3]
        icon = "🟢" if status == "Completed" else "🟡"
        rows_kb.append([InlineKeyboardButton(text=f"{icon} #{api_id} ({qty}) — {status}", callback_data=f"ord:{api_id}")])

    rows_kb.append([InlineKeyboardButton(text="🔙 Orqaga", callback_data="menu:home")])
    await safe_edit(call, "📦 <b>Buyurtmalaringiz:</b>", InlineKeyboardMarkup(inline_keyboard=rows_kb))


@router.callback_query(F.data.startswith("ord:"))
async def cb_order_detail(call: CallbackQuery):
    api_order_id = int(call.data.split(":")[1])
    if USE_POSTGRES:
        import asyncpg
        conn = await asyncpg.connect(DATABASE_URL)
        try:
            row = await conn.fetchrow("SELECT service_name, link, quantity, charge, status FROM orders WHERE user_id = $1 AND api_order_id = $2", call.from_user.id, api_order_id)
        finally:
            await conn.close()
    else:
        async with aiosqlite.connect("reseller_smm.db") as db:
            async with db.execute("SELECT service_name, link, quantity, charge, status FROM orders WHERE user_id = ? AND api_order_id = ?", (call.from_user.id, api_order_id)) as cur:
                row = await cur.fetchone()

    if not row:
        return await call.answer("Topilmadi", show_alert=True)

    s_name, link, qty, charge, status = row if not USE_POSTGRES else (row["service_name"], row["link"], row["quantity"], row["charge"], row["status"])

    api_res = await smm_request({"action": "status", "order": api_order_id})
    if isinstance(api_res, dict) and "status" in api_res:
        status = api_res["status"]
        if status == "Canceled":
            await update_user_balance(call.from_user.id, charge)

    text = f"📦 <b>Buyurtma #{api_order_id}</b>\n📌 {s_name}\n🔗 {link}\n📊 Miqdor: {qty}\n💰 Summa: {charge:,.2f} UZS\n📊 Holati: <b>{status}</b>"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Yangilash", callback_data=f"ord:{api_order_id}")],
        [InlineKeyboardButton(text="🔙 Orqaga", callback_data="orders:0")]
    ])
    await safe_edit(call, text, kb)


@router.callback_query(F.data == "menu:topup")
async def cb_topup(call: CallbackQuery, state: FSMContext):
    await state.clear()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⭐️ 50 Stars", callback_data="buy_stars:50"), InlineKeyboardButton(text="⭐️ 100 Stars", callback_data="buy_stars:100")],
        [InlineKeyboardButton(text="🔙 Orqaga", callback_data="menu:home")]
    ])
    await safe_edit(call, "⭐️ Telegram Stars orqali to'ldiring:", kb)


@router.callback_query(F.data.startswith("buy_stars:"))
async def cb_buy_stars(call: CallbackQuery):
    stars = int(call.data.split(":")[1])
    uzs = stars * UZS_PER_STAR
    prices = [LabeledPrice(label=f"{uzs:,} UZS", amount=stars)]
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"⭐️ To'lash ({stars} Stars)", pay=True)],
        [InlineKeyboardButton(text="🔙 Orqaga", callback_data="menu:home")]
    ])
    await call.message.answer_invoice(
        title="Balansni to'ldirish",
        description=f"{uzs:,} UZS qo'shiladi",
        payload=f"topup:{stars}:{uzs}",
        currency="XTR",
        prices=prices,
        provider_token="",
        reply_markup=kb
    )
    await call.answer()


@router.pre_checkout_query()
async def pre_checkout(pc: PreCheckoutQuery):
    await pc.answer(ok=True)


@router.message(F.successful_payment)
async def successful_payment(message: Message):
    _, _, uzs_str = message.successful_payment.invoice_payload.split(":")
    uzs = float(uzs_str)
    await update_user_balance(message.from_user.id, uzs)
    await message.answer(f"✅ Hisobingizga +{uzs:,.2f} UZS qo'shildi!", reply_markup=main_menu_kb(message.from_user.id == ADMIN_ID))


# ==================== ADMIN PANEL & COMMANDS ====================
@router.callback_query(F.data == "admin:panel")
async def cb_admin_panel(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return await call.answer("Ruxsat yo'q!", show_alert=True)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Statistika (/stats)", callback_data="admin:stats")],
        [InlineKeyboardButton(text="📦 Buyurtmalar (/orders)", callback_data="admin:orders")],
        [InlineKeyboardButton(text="📢 Xabar tarqatish (/broadcast)", callback_data="admin:broadcast")],
        [InlineKeyboardButton(text="🔙 Asosiy menyu", callback_data="menu:home")]
    ])
    await safe_edit(call, "⚡️ <b>Admin Boshqaruv Paneli</b>", kb)


@router.callback_query(F.data == "admin:stats")
async def cb_admin_stats(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return
    if USE_POSTGRES:
        import asyncpg
        conn = await asyncpg.connect(DATABASE_URL)
        try:
            ucount = await conn.fetchval("SELECT COUNT(*) FROM users")
            ocount = await conn.fetchval("SELECT COUNT(*) FROM orders")
        finally:
            await conn.close()
    else:
        async with aiosqlite.connect("reseller_smm.db") as db:
            async with db.execute("SELECT COUNT(*) FROM users") as cur:
                ucount = (await cur.fetchone())[0]
            async with db.execute("SELECT COUNT(*) FROM orders") as cur:
                ocount = (await cur.fetchone())[0]

    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Orqaga", callback_data="admin:panel")]])
    await safe_edit(call, f"📊 Statistika:\n- Foydalanuvchilar: {ucount}\n- Buyurtmalar: {ocount}", kb)


@router.callback_query(F.data == "admin:orders")
async def cb_admin_orders(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return
    if USE_POSTGRES:
        import asyncpg
        conn = await asyncpg.connect(DATABASE_URL)
        try:
            rows = await conn.fetch("SELECT api_order_id, user_id, status FROM orders ORDER BY id DESC LIMIT 10")
        finally:
            await conn.close()
    else:
        async with aiosqlite.connect("reseller_smm.db") as db:
            async with db.execute("SELECT api_order_id, user_id, status FROM orders ORDER BY id DESC LIMIT 10") as cur:
                rows = await cur.fetchall()

    text = "📦 So'nggi 10 ta buyurtma:\n"
    for r in rows:
        text += f"#{r[0] if not USE_POSTGRES else r['api_order_id']} | User: {r[1] if not USE_POSTGRES else r['user_id']} | {r[2] if not USE_POSTGRES else r['status']}\n"
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Orqaga", callback_data="admin:panel")]])
    await safe_edit(call, text, kb)


@router.callback_query(F.data == "admin:broadcast")
async def cb_admin_broadcast(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        return
    await state.set_state(BroadcastFlow.waiting_message)
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Bekor qilish", callback_data="admin:panel")]])
    await safe_edit(call, "📢 Tarqatish uchun xabarni yuboring:", kb)


@router.message(BroadcastFlow.waiting_message, F.from_user.id == ADMIN_ID)
async def process_broadcast(message: Message, state: FSMContext):
    await state.clear()
    if USE_POSTGRES:
        import asyncpg
        conn = await asyncpg.connect(DATABASE_URL)
        try:
            users = await conn.fetch("SELECT user_id FROM users")
        finally:
            await conn.close()
        user_ids = [u["user_id"] for u in users]
    else:
        async with aiosqlite.connect("reseller_smm.db") as db:
            async with db.execute("SELECT user_id FROM users") as cur:
                user_ids = [row[0] for row in await cur.fetchall()]

    for uid in user_ids:
        try:
            await message.send_copy(chat_id=uid)
            await asyncio.sleep(0.04)
        except Exception:
            pass
    await message.answer("✅ Tarqatish tugadi!")


@router.message(Command("stats"), F.from_user.id == ADMIN_ID)
async def cmd_stats(message: Message):
    if USE_POSTGRES:
        import asyncpg
        conn = await asyncpg.connect(DATABASE_URL)
        try:
            ucount = await conn.fetchval("SELECT COUNT(*) FROM users")
        finally:
            await conn.close()
    else:
        async with aiosqlite.connect("reseller_smm.db") as db:
            async with db.execute("SELECT COUNT(*) FROM users") as cur:
                ucount = (await cur.fetchone())[0]
    await message.answer(f"👥 Jami foydalanuvchilar: {ucount}")


@router.message(Command("orders"), F.from_user.id == ADMIN_ID)
async def cmd_orders(message: Message):
    if USE_POSTGRES:
        import asyncpg
        conn = await asyncpg.connect(DATABASE_URL)
        try:
            rows = await conn.fetch("SELECT api_order_id, status FROM orders ORDER BY id DESC LIMIT 10")
        finally:
            await conn.close()
    else:
        async with aiosqlite.connect("reseller_smm.db") as db:
            async with db.execute("SELECT api_order_id, status FROM orders ORDER BY id DESC LIMIT 10") as cur:
                rows = await cur.fetchall()
    text = "📦 Oxirgi buyurtmalar:\n" + "\n".join([f"#{r[0]} - {r[1]}" for r in rows])
    await message.answer(text)


@router.message(Command("broadcast"), F.from_user.id == ADMIN_ID)
async def cmd_broadcast_cmd(message: Message, state: FSMContext):
    await state.set_state(BroadcastFlow.waiting_message)
    await message.answer("📢 Tarqatish uchun xabarni kiriting:")


@router.message(Command("add"), F.from_user.id == ADMIN_ID)
async def cmd_add(message: Message):
    parts = message.text.split()
    if len(parts) < 3:
        return await message.answer("Format: /add <miqdor> <userid>")
    await update_user_balance(int(parts[2]), float(parts[1]))
    await message.answer(f"✅ Balansga qo'shildi!")


@router.callback_query(F.data == "noop")
async def cb_noop(call: CallbackQuery):
    await call.answer()


async def health_check(request):
    return web.json_response({"status": "online"})


async def main():
    if not BOT_TOKEN or not SMM_API_KEY:
        logger.error("Tokenlar kiritilmagan!")
    await init_db()
    app = web.Application()
    app.router.add_get("/", health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=PORT)
    await site.start()

    await get_services(force=True)
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
