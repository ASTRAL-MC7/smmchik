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

# ==================== CONFIGURATION & ENV ====================
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
SMM_API_KEY = os.getenv("SMM_API_KEY", "")
SMM_API_URL = "https://kometa-smm.uz/api/v2"

ADMIN_ID = 5523761749

# Pricing Settings
MARKUP = 1.70  # +70% Reseller Markup (e.g., 1000 UZS becomes 1700 UZS)
UZS_PER_STAR = int(os.getenv("UZS_PER_STAR", "250"))
PORT = int(os.getenv("PORT", "8080"))
DB_PATH = "reseller_smm.db"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ==================== COLORFUL & PREMIUM EMOJIS ====================
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
E_GEAR = "⚡️"
E_BACK = "🔙"
E_BOX = "📦"
E_REFRESH = "🔄"
E_LINK = "🔗"
E_HASH = "🔢"

# ==================== CACHE & DATABASE ====================
services_cache = {
    "data": [],
    "platforms": {"Instagram": [], "Telegram": [], "YouTube": [], "TikTok": [], "Boshqa": []},
    "by_id": {},
    "last_updated": 0
}
CACHE_TTL = 600  # 10 minutes cache


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
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
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT balance FROM users WHERE user_id = ?", (user_id,)) as cur:
            row = await cur.fetchone()
            if not row:
                await db.execute("INSERT INTO users (user_id, balance) VALUES (?, 0.0)", (user_id,))
                await db.commit()
                return 0.0
            return round(float(row[0]), 2)


async def update_user_balance(user_id: int, delta: float):
    async with aiosqlite.connect(DB_PATH) as db:
        await get_user_balance(user_id)  # Ensure user exists
        if delta < 0:
            await db.execute(
                "UPDATE users SET balance = balance + ?, total_spent = total_spent + ? WHERE user_id = ?",
                (delta, abs(delta), user_id)
            )
        else:
            await db.execute(
                "UPDATE users SET balance = balance + ? WHERE user_id = ?",
                (delta, user_id)
            )
        await db.commit()


# ==================== KOMETA SMM API CLIENT ====================
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


# ==================== FSM STATES ====================
class OrderFlow(StatesGroup):
    waiting_link = State()
    waiting_quantity = State()


class TopUpFlow(StatesGroup):
    waiting_custom_stars = State()


class BroadcastFlow(StatesGroup):
    waiting_message = State()


# ==================== KEYBOARD BUILDERS ====================
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


# ==================== ROUTER & HANDLERS ====================
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
        f"{E_ROCKET} <b>Premium SMM Reseller Botga</b> xush kelibsiz!\n\n"
        f"{E_BAG} <b>Balansingiz:</b> <code>{bal:,.2f} UZS</code>\n"
        f"{E_STAR} <b>To'lov:</b> Telegram Stars (24/7 Avto)\n\n"
        f"{E_GEAR} <i>Kerakli bo'limni tanlang:</i>"
    )
    await message.answer(text, reply_markup=main_menu_kb(is_admin))


@router.callback_query(F.data == "menu:home")
async def cb_home(call: CallbackQuery, state: FSMContext):
    await state.clear()
    bal = await get_user_balance(call.from_user.id)
    is_admin = (call.from_user.id == ADMIN_ID)
    text = (
        f"{E_FIRE} <b>Asosiy Menyu</b>\n\n"
        f"{E_BAG} <b>Balansingiz:</b> <code>{bal:,.2f} UZS</code>\n"
        f"{E_STAR} <b>Kurs:</b> 1 ⭐️ = <code>{UZS_PER_STAR:,} UZS</code>"
    )
    await safe_edit(call, text, main_menu_kb(is_admin))


@router.callback_query(F.data == "menu:balance")
async def cb_balance(call: CallbackQuery):
    bal = await get_user_balance(call.from_user.id)
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT total_spent FROM users WHERE user_id = ?", (call.from_user.id,)) as cur:
            row = await cur.fetchone()
            spent = float(row[0]) if row else 0.0
        async with db.execute("SELECT COUNT(*) FROM orders WHERE user_id = ?", (call.from_user.id,)) as cur:
            cnt = (await cur.fetchone())[0]

    text = (
        f"{E_DIAMOND} <b>Shaxsiy Kabinet</b>\n\n"
        f"🆔 <b>ID:</b> <code>{call.from_user.id}</code>\n"
        f"{E_BAG} <b>Balans:</b> <code>{bal:,.2f} UZS</code>\n"
        f"📉 <b>Sarflangan:</b> <code>{spent:,.2f} UZS</code>\n"
        f"{E_BOX} <b>Buyurtmalar:</b> <code>{cnt} ta</code>"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"{E_YELLOW} ⭐️ Balansni to'ldirish", callback_data="menu:topup")],
        [InlineKeyboardButton(text=f"{E_BACK} 🔙 Orqaga", callback_data="menu:home")]
    ])
    await safe_edit(call, text, kb)


@router.callback_query(F.data == "menu:help")
async def cb_help(call: CallbackQuery):
    text = (
        f"{E_DIAMOND} <b>Qoidalar va Yo'riqnoma</b>\n\n"
        f"1️⃣ Xizmatni tanlang, ochiq profil/post havolasini yuboring va miqdorni kiriting.\n"
        f"2️⃣ Agar buyurtma bekor qilinsa (<i>Canceled</i>) yoki qisman bajarilsa (<i>Partial</i>), mablag'ingiz avtomatik qaytariladi.\n"
        f"3️⃣ To'lovlar Telegram Stars ({E_STAR}) orqali 24/7 ishlaydi."
    )
    await safe_edit(call, text, back_home_kb())


# ==================== CATALOG & PLATFORMS ====================
@router.callback_query(F.data == "catalog:main")
async def cb_catalog_main(call: CallbackQuery, state: FSMContext):
    await state.clear()
    cache = await get_services()
    platforms = cache["platforms"]

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text=f"📸 Instagram ({len(platforms['Instagram'])})", callback_data="plat:Instagram:0"),
            InlineKeyboardButton(text=f"✈️ Telegram ({len(platforms['Telegram'])})", callback_data="plat:Telegram:0")
        ],
        [
            InlineKeyboardButton(text=f"▶️ YouTube ({len(platforms['YouTube'])})", callback_data="plat:YouTube:0"),
            InlineKeyboardButton(text=f"🎵 TikTok ({len(platforms['TikTok'])})", callback_data="plat:TikTok:0")
        ],
        [
            InlineKeyboardButton(text=f"✨ Boshqa xizmatlar ({len(platforms['Boshqa'])})", callback_data="plat:Boshqa:0")
        ],
        [
            InlineKeyboardButton(text=f"{E_BACK} 🔙 Asosiy menyu", callback_data="menu:home")
        ]
    ])
    await safe_edit(
        call,
        f"{E_CART} <b>SMM Xizmatlar Katalogi</b>\n\n"
        f"<i>Ijtimoiy tarmoq turini tanlang:</i>",
        kb
    )


@router.callback_query(F.data.startswith("plat:"))
async def cb_platform_services(call: CallbackQuery):
    _, plat_name, page_str = call.data.split(":")
    page = int(page_str)

    cache = await get_services()
    srv_list = cache["platforms"].get(plat_name, [])

    if not srv_list:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"{E_BACK} 🔙 Platformalarga qaytish", callback_data="catalog:main")]
        ])
        return await safe_edit(call, f"{E_CROSS} Bu bo'limda hozircha xizmatlar mavjud emas.", kb)

    per_page = 6
    total_pages = (len(srv_list) + per_page - 1) // per_page
    start = page * per_page
    end = start + per_page
    current_srvs = srv_list[start:end]

    rows = []
    for s in current_srvs:
        sid = s["service"]
        name = s["name"]
        rate = s["reseller_rate"]
        btn_label = f"{E_GREEN} #{sid} | {name[:24]} ({rate:,.0f} UZS)"
        rows.append([InlineKeyboardButton(text=btn_label, callback_data=f"srv:{sid}:{plat_name}:{page}")])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"plat:{plat_name}:{page-1}"))
    nav.append(InlineKeyboardButton(text=f"📄 {page+1}/{total_pages}", callback_data="noop"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton(text="➡️", callback_data=f"plat:{plat_name}:{page+1}"))
    if nav:
        rows.append(nav)

    rows.append([InlineKeyboardButton(text=f"{E_BACK} 🔙 Platformalarga qaytish", callback_data="catalog:main")])

    await safe_edit(
        call,
        f"📌 <b>Platforma: {plat_name}</b>\n"
        f"<i>Narxlar 1,000 dona uchun (+70% Reseller). Xizmatni tanlang:</i>",
        InlineKeyboardMarkup(inline_keyboard=rows)
    )


@router.callback_query(F.data.startswith("srv:"))
async def cb_service_detail(call: CallbackQuery, state: FSMContext):
    await state.clear()
    parts = call.data.split(":")
    sid = int(parts[1])
    plat_name = parts[2] if len(parts) > 2 else "Instagram"
    page = int(parts[3]) if len(parts) > 3 else 0

    cache = await get_services()
    s = cache["by_id"].get(sid)
    if not s:
        return await call.answer("Xizmat topilmadi!", show_alert=True)

    refill_icon = E_GREEN if s.get("refill") else E_RED
    cancel_icon = E_GREEN if s.get("cancel") else E_RED

    text = (
        f"{E_ROCKET} <b>#{s['service']} — {s['name']}</b>\n\n"
        f"📂 <b>Kategoriya:</b> {s['category']}\n"
        f"{E_BAG} <b>Narxi (1000 ta):</b> <code>{s['reseller_rate']:,.2f} UZS</code>\n"
        f"⬇️ <b>Min:</b> <code>{s['min']}</code> | ⬆️ <b>Max:</b> <code>{s['max']}</code>\n"
        f"{refill_icon} <b>Refill (Qayta tiklash):</b> {'Mavjud' if s.get('refill') else 'Yoq'}\n"
        f"{cancel_icon} <b>Cancel (Bekor qilish):</b> {'Mavjud' if s.get('cancel') else 'Yoq'}"
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"{E_GREEN} ✅ Buyurtma berish", callback_data=f"order_start:{sid}")],
        [InlineKeyboardButton(text=f"{E_BACK} 🔙 Orqaga", callback_data=f"plat:{plat_name}:{page}")]
    ])
    await safe_edit(call, text, kb)


# ==================== ORDER PLACEMENT ====================
@router.callback_query(F.data.startswith("order_start:"))
async def cb_order_start(call: CallbackQuery, state: FSMContext):
    sid = int(call.data.split(":")[1])
    cache = await get_services()
    s = cache["by_id"].get(sid)
    if not s:
        return await call.answer("Xizmat topilmadi!", show_alert=True)

    await state.set_state(OrderFlow.waiting_link)
    await state.update_data(service_id=sid)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"{E_RED} ❌ Bekor qilish", callback_data="catalog:main")]
    ])
    await safe_edit(
        call,
        f"{E_LINK} <b>Havolani yuboring (Link):</b>\n\n"
        f"📌 <b>Xizmat:</b> {s['name']}\n"
        f"⚠️ <i>Profil yoki post havolasi https:// bilan boshlanishi kerak.</i>",
        kb
    )


@router.message(OrderFlow.waiting_link)
async def msg_order_link(message: Message, state: FSMContext):
    link = (message.text or "").strip()
    data = await state.get_data()
    sid = data["service_id"]

    kb_cancel = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"{E_RED} ❌ Bekor qilish", callback_data="catalog:main")]
    ])

    if not (link.startswith("http://") or link.startswith("https://")) or len(link) > 500:
        return await message.answer(
            f"{E_RED} <b>Xato havola!</b> Link <code>https://</code> bilan boshlanishi shart.",
            reply_markup=kb_cancel
        )

    cache = await get_services()
    s = cache["by_id"].get(sid)

    await state.update_data(link=link)
    await state.set_state(OrderFlow.waiting_quantity)

    await message.answer(
        f"{E_HASH} <b>Miqdorni kiriting (soni):</b>\n\n"
        f"⬇️ Min: <code>{s['min']}</code> | ⬆️ Max: <code>{s['max']}</code>\n"
        f"{E_BAG} Narx (1000 ta): <code>{s['reseller_rate']:,.2f} UZS</code>",
        reply_markup=kb_cancel
    )


@router.message(OrderFlow.waiting_quantity)
async def msg_order_quantity(message: Message, state: FSMContext):
    data = await state.get_data()
    sid = data["service_id"]
    link = data["link"]

    kb_cancel = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"{E_RED} ❌ Bekor qilish", callback_data="catalog:main")]
    ])

    if not message.text or not message.text.isdigit():
        return await message.answer(f"{E_RED} Iltimos, faqat raqam kiriting!", reply_markup=kb_cancel)

    qty = int(message.text)
    cache = await get_services()
    s = cache["by_id"].get(sid)
    min_q, max_q = int(s["min"]), int(s["max"])

    if qty < min_q or qty > max_q:
        return await message.answer(
            f"{E_RED} Miqdor <code>{min_q}</code> va <code>{max_q}</code> oralig'ida bo'lishi kerak!",
            reply_markup=kb_cancel
        )

    total_charge = round((qty / 1000.0) * s["reseller_rate"], 2)
    bal = await get_user_balance(message.from_user.id)

    await state.update_data(quantity=qty, charge=total_charge)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"{E_GREEN} ✅ Tasdiqlash va Yuborish", callback_data="order_confirm")],
        [InlineKeyboardButton(text=f"{E_RED} ❌ Bekor qilish", callback_data="catalog:main")]
    ])

    await message.answer(
        f"{E_DIAMOND} <b>Buyurtmani tasdiqlang:</b>\n\n"
        f"📌 <b>Xizmat:</b> #{sid} — {s['name']}\n"
        f"{E_LINK} <b>Link:</b> <code>{link}</code>\n"
        f"{E_HASH} <b>Miqdor:</b> <code>{qty:,} ta</code>\n"
        f"{E_BAG} <b>Summa:</b> <code>{total_charge:,.2f} UZS</code>\n"
        f"💳 <b>Balansingiz:</b> <code>{bal:,.2f} UZS</code>",
        reply_markup=kb
    )


@router.callback_query(F.data == "order_confirm")
async def cb_order_confirm(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if not data or "service_id" not in data:
        return await call.answer("Ma'lumot eskirgan!", show_alert=True)

    sid = data["service_id"]
    link = data["link"]
    qty = data["quantity"]
    charge = data["charge"]
    await state.clear()

    bal = await get_user_balance(call.from_user.id)
    if bal < charge:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"{E_YELLOW} ⭐️ Hisobni to'ldirish", callback_data="menu:topup")],
            [InlineKeyboardButton(text=f"{E_BACK} 🔙 Asosiy menyu", callback_data="menu:home")]
        ])
        return await safe_edit(
            call,
            f"{E_RED} <b>Balansingiz yetarli emas!</b>\n\n"
            f"Kerak: <code>{charge:,.2f} UZS</code>\n"
            f"Sizda: <code>{bal:,.2f} UZS</code>",
            kb
        )

    await update_user_balance(call.from_user.id, -charge)

    res = await smm_request({
        "action": "add",
        "service": sid,
        "link": link,
        "quantity": qty
    })

    if isinstance(res, dict) and "order" in res:
        api_order_id = int(res["order"])
        cache = await get_services()
        s_name = cache["by_id"].get(sid, {}).get("name", f"Service #{sid}")

        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "INSERT INTO orders (user_id, api_order_id, service_id, service_name, link, quantity, charge, status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'Pending')",
                (call.from_user.id, api_order_id, sid, s_name, link, qty, charge)
            )
            await db.commit()

        new_bal = await get_user_balance(call.from_user.id)
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"{E_BLUE} 📦 Buyurtmani kuzatish", callback_data=f"ord:{api_order_id}")],
            [InlineKeyboardButton(text=f"{E_BACK} 🔙 Asosiy menyu", callback_data="menu:home")]
        ])
        await safe_edit(
            call,
            f"{E_CHECK} <b>Buyurtma muvaffaqiyatli qabul qilindi!</b>\n\n"
            f"🆔 ID: <code>#{api_order_id}</code>\n"
            f"📌 {s_name}\n"
            f"📊 Miqdor: <code>{qty:,} ta</code>\n"
            f"💰 Yechildi: <code>{charge:,.2f} UZS</code>\n"
            f"💳 Qoldiq balans: <code>{new_bal:,.2f} UZS</code>",
            kb
        )
    else:
        # Automatic refund on failure
        await update_user_balance(call.from_user.id, charge)
        err = res.get("error", "Noma'lum xatolik") if isinstance(res, dict) else "Server xatosi"
        await safe_edit(
            call,
            f"{E_RED} <b>Buyurtma bajarilmadi:</b> <code>{err}</code>\n"
            f"{E_CHECK} <i>Summangiz ({charge:,.2f} UZS) balansingizga to'liq qaytarildi.</i>",
            back_home_kb()
        )


# ==================== ORDERS HISTORY & STATUS ====================
@router.callback_query(F.data.startswith("orders:"))
async def cb_orders_list(call: CallbackQuery):
    page = int(call.data.split(":")[1])
    per_page = 6
    offset = page * per_page

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT COUNT(*) FROM orders WHERE user_id = ?", (call.from_user.id,)) as cur:
            total = (await cur.fetchone())[0]
        async with db.execute(
            "SELECT api_order_id, service_name, quantity, charge, status FROM orders "
            "WHERE user_id = ? ORDER BY id DESC LIMIT ? OFFSET ?",
            (call.from_user.id, per_page, offset)
        ) as cur:
            rows_db = await cur.fetchall()

    if total == 0:
        return await safe_edit(call, f"{E_BOX} Sizda hali buyurtmalar yo'q.", back_home_kb())

    total_pages = (total + per_page - 1) // per_page
    kb_rows = []
    for api_id, s_name, qty, charge, status in rows_db:
        icon = E_GREEN if status == "Completed" else (E_RED if status in ("Canceled", "Refunded") else E_YELLOW)
        kb_rows.append([InlineKeyboardButton(
            text=f"{icon} #{api_id} | {s_name[:16]} ({qty}) — {status}",
            callback_data=f"ord:{api_id}"
        )])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"orders:{page-1}"))
    nav.append(InlineKeyboardButton(text=f"📄 {page+1}/{total_pages}", callback_data="noop"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton(text="➡️", callback_data=f"orders:{page+1}"))
    if nav:
        kb_rows.append(nav)

    kb_rows.append([InlineKeyboardButton(text=f"{E_BACK} 🔙 Asosiy menyu", callback_data="menu:home")])
    await safe_edit(call, f"{E_BOX} <b>Sizning buyurtmalaringiz:</b>", InlineKeyboardMarkup(inline_keyboard=kb_rows))


@router.callback_query(F.data.startswith("ord:"))
async def cb_order_detail(call: CallbackQuery):
    api_order_id = int(call.data.split(":")[1])

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT service_name, link, quantity, charge, status FROM orders WHERE user_id = ? AND api_order_id = ?",
            (call.from_user.id, api_order_id)
        ) as cur:
            row = await cur.fetchone()

    if not row:
        return await call.answer("Buyurtma topilmadi!", show_alert=True)

    s_name, link, qty, charge, db_status = row

    api_res = await smm_request({"action": "status", "order": api_order_id})
    start_count = "-"
    remains = "-"
    live_status = db_status
    refund_note = ""

    if isinstance(api_res, dict) and "status" in api_res:
        live_status = api_res["status"]
        start_count = api_res.get("start_count", "-")
        remains = api_res.get("remains", "-")

        if live_status == "Canceled" and db_status not in ("Canceled", "Refunded"):
            await update_user_balance(call.from_user.id, charge)
            live_status = "Refunded"
            refund_note = f"\n\n{E_GREEN} <b>Bekor qilindi, {charge:,.2f} UZS balansingizga qaytarildi!</b>"
        elif live_status == "Partial" and db_status not in ("Partial", "Refunded"):
            rem_int = int(remains) if str(remains).isdigit() else 0
            if 0 < rem_int <= qty:
                refund_amt = round((rem_int / qty) * charge, 2)
                await update_user_balance(call.from_user.id, refund_amt)
                refund_note = f"\n\n{E_GREEN} <b>Qisman bajarildi: {refund_amt:,.2f} UZS qaytarildi!</b>"

        if live_status != db_status:
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute("UPDATE orders SET status = ? WHERE api_order_id = ?", (live_status, api_order_id))
                await db.commit()

    text = (
        f"{E_BOX} <b>Buyurtma #{api_order_id}</b>\n\n"
        f"📌 {s_name}\n"
        f"🔗 <code>{link}</code>\n"
        f"📊 Miqdor: <code>{qty:,} ta</code>\n"
        f"▶️ Boshlang'ich: {start_count} | ⏳ Qoldiq: {remains}\n"
        f"💰 Summa: <code>{charge:,.2f} UZS</code>\n"
        f"📊 Holati: <b>{live_status}</b>{refund_note}"
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"{E_REFRESH} Yangilash", callback_data=f"ord:{api_order_id}")],
        [
            InlineKeyboardButton(text="♻️ Refill", callback_data=f"act_refill:{api_order_id}"),
            InlineKeyboardButton(text="🚫 Cancel", callback_data=f"act_cancel:{api_order_id}")
        ],
        [InlineKeyboardButton(text=f"{E_BACK} 🔙 Ro'yxat", callback_data="orders:0")]
    ])
    await safe_edit(call, text, kb)


@router.callback_query(F.data.startswith("act_refill:"))
async def cb_refill(call: CallbackQuery):
    res = await smm_request({"action": "refill", "order": call.data.split(":")[1]})
    if isinstance(res, dict) and "error" not in res:
        await call.answer("✅ Refill so'rovi yuborildi!", show_alert=True)
    else:
        await call.answer(f"❌ {res.get('error', 'Xatolik') if isinstance(res, dict) else 'Xato'}", show_alert=True)


@router.callback_query(F.data.startswith("act_cancel:"))
async def cb_cancel(call: CallbackQuery):
    res = await smm_request({"action": "cancel", "orders": call.data.split(":")[1]})
    if isinstance(res, dict) and "error" in res:
        await call.answer(f"❌ {res['error']}", show_alert=True)
    else:
        await call.answer("✅ Bekor qilish so'rovi yuborildi!", show_alert=True)


# ==================== TELEGRAM STARS TOP-UP ====================
@router.callback_query(F.data == "menu:topup")
async def cb_topup(call: CallbackQuery, state: FSMContext):
    await state.clear()
    packages = [10, 25, 50, 100, 250, 500]
    rows = []
    row = []
    for stars in packages:
        uzs = stars * UZS_PER_STAR
        row.append(InlineKeyboardButton(text=f"⭐️ {stars} Stars ({uzs:,} UZS)", callback_data=f"buy_stars:{stars}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton(text=f"{E_HASH} Boshqa miqdor", callback_data="buy_stars:custom")])
    rows.append([InlineKeyboardButton(text=f"{E_BACK} 🔙 Orqaga", callback_data="menu:home")])

    await safe_edit(
        call,
        f"{E_STAR} <b>Telegram Stars orqali to'ldirish</b>\n\n"
        f"1 ⭐️ = <code>{UZS_PER_STAR:,} UZS</code>",
        InlineKeyboardMarkup(inline_keyboard=rows)
    )


@router.callback_query(F.data.startswith("buy_stars:"))
async def cb_buy_stars(call: CallbackQuery, state: FSMContext):
    val = call.data.split(":")[1]
    if val == "custom":
        await state.set_state(TopUpFlow.waiting_custom_stars)
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=f"{E_BACK} Bekor qilish", callback_data="menu:topup")]])
        return await safe_edit(call, "Nechta Stars to'lamoqchisiz? (1 - 10000)", kb)

    await send_stars_invoice(call.message, int(val))
    await call.answer()


@router.message(TopUpFlow.waiting_custom_stars)
async def msg_custom_stars(message: Message, state: FSMContext):
    if not message.text or not message.text.isdigit():
        return await message.answer("Faqat butun son kiriting!")
    stars = int(message.text)
    await state.clear()
    await send_stars_invoice(message, stars)


async def send_stars_invoice(message: Message, stars: int):
    uzs = stars * UZS_PER_STAR
    prices = [LabeledPrice(label=f"{uzs:,} UZS", amount=stars)]
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"⭐️ To'lash ({stars} Stars)", pay=True)],
        [InlineKeyboardButton(text=f"🔙 Orqaga", callback_data="menu:home")]
    ])
    await message.answer_invoice(
        title="SMM Balansni To'ldirish",
        description=f"Hisobingizga {uzs:,} UZS qo'shiladi.",
        payload=f"topup:{stars}:{uzs}",
        currency="XTR",
        prices=prices,
        provider_token="",
        reply_markup=kb
    )


@router.pre_checkout_query()
async def pre_checkout(pc: PreCheckoutQuery):
    await pc.answer(ok=True)


@router.message(F.successful_payment)
async def successful_payment(message: Message):
    payload = message.successful_payment.invoice_payload
    if payload.startswith("topup:"):
        _, _, uzs_str = payload.split(":")
        uzs = float(uzs_str)
        await update_user_balance(message.from_user.id, uzs)
        bal = await get_user_balance(message.from_user.id)
        await message.answer(f"✅ To'lov qabul qilindi! Balans: <code>{bal:,.2f} UZS</code>", reply_markup=main_menu_kb(message.from_user.id == ADMIN_ID))


# ==================== ADMIN COMMANDS & PANEL ====================
@router.callback_query(F.data == "admin:panel")
async def cb_admin_panel(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return await call.answer("Ruxsat yo'q!", show_alert=True)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Statistika (/stats)", callback_data="admin:stats")],
        [InlineKeyboardButton(text="📦 So'nggi buyurtmalar (/orders)", callback_data="admin:orders")],
        [InlineKeyboardButton(text="📢 Xabar tarqatish (/broadcast)", callback_data="admin:broadcast")],
        [InlineKeyboardButton(text=f"{E_BACK} 🔙 Asosiy menyu", callback_data="menu:home")]
    ])
    await safe_edit(call, f"{E_RED} ⚡️ <b>Admin Boshqaruv Paneli</b>", kb)


@router.callback_query(F.data == "admin:stats")
async def cb_admin_stats(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return await call.answer("Ruxsat yo'q!", show_alert=True)

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT COUNT(*) FROM users") as cur:
            users_count = (await cur.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM orders") as cur:
            orders_count = (await cur.fetchone())[0]
        async with db.execute("SELECT SUM(total_spent) FROM users") as cur:
            row = await cur.fetchone()
            total_spent = row[0] if row and row[0] else 0.0

    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Orqaga", callback_data="admin:panel")]])
    await safe_edit(
        call,
        f"📊 <b>Bot Statistikasi:</b>\n\n"
        f"👥 Foydalanuvchilar: <code>{users_count} ta</code>\n"
        f"📦 Jami buyurtmalar: <code>{orders_count} ta</code>\n"
        f"💰 Jami aylanma: <code>{total_spent:,.2f} UZS</code>",
        kb
    )


@router.callback_query(F.data == "admin:orders")
async def cb_admin_orders(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return await call.answer("Ruxsat yo'q!", show_alert=True)

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT api_order_id, user_id, service_name, quantity, status FROM orders ORDER BY id DESC LIMIT 10") as cur:
            rows = await cur.fetchall()

    text = f"📦 <b>So'nggi 10 ta Buyurtma:</b>\n\n"
    for r in rows:
        api_id, u_id, s_name, qty, status = r
        text += f"▪️ #{api_id} | User: <code>{u_id}</code> | {s_name[:12]} ({qty}) — <b>{status}</b>\n"

    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Orqaga", callback_data="admin:panel")]])
    await safe_edit(call, text, kb)


@router.callback_query(F.data == "admin:broadcast")
async def cb_admin_broadcast(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        return await call.answer("Ruxsat yo'q!", show_alert=True)

    await state.set_state(BroadcastFlow.waiting_message)
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Bekor qilish", callback_data="admin:panel")]])
    await safe_edit(call, f"📢 <b>Barcha foydalanuvchilarga yuboriladigan xabarni kiriting:</b>", kb)


@router.message(BroadcastFlow.waiting_message, F.from_user.id == ADMIN_ID)
async def process_broadcast(message: Message, state: FSMContext, bot: Bot):
    await state.clear()
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT user_id FROM users") as cur:
            user_ids = [row[0] for row in await cur.fetchall()]

    success = 0
    fail = 0
    status_msg = await message.answer(f"📢 Tarqatish boshlandi ({len(user_ids)} ta foydalanuvchi)...")

    for uid in user_ids:
        try:
            await message.send_copy(chat_id=uid)
            success += 1
            await asyncio.sleep(0.05)
        except Exception:
            fail += 1

    await status_msg.edit_text(f"✅ <b>Tarqatish yakunlandi!</b>\n\n- Yuborildi: {success}\n- Xatolik: {fail}")


@router.message(Command("stats"), F.from_user.id == ADMIN_ID)
async def cmd_stats(message: Message):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT COUNT(*) FROM users") as cur:
            users_count = (await cur.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM orders") as cur:
            orders_count = (await cur.fetchone())[0]

    await message.answer(f"📊 <b>Statistika:</b>\nFoydalanuvchilar: {users_count}\nBuyurtmalar: {orders_count}")


@router.message(Command("orders"), F.from_user.id == ADMIN_ID)
async def cmd_admin_orders(message: Message):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT api_order_id, user_id, service_name, quantity, status FROM orders ORDER BY id DESC LIMIT 10") as cur:
            rows = await cur.fetchall()

    text = "📦 <b>So'nggi 10 ta buyurtma:</b>\n\n"
    for r in rows:
        api_id, u_id, s_name, qty, status = r
        text += f"#{api_id} | {u_id} | {s_name[:12]} ({qty}) — {status}\n"
    await message.answer(text)


@router.message(Command("broadcast"), F.from_user.id == ADMIN_ID)
async def cmd_broadcast(message: Message, state: FSMContext):
    await state.set_state(BroadcastFlow.waiting_message)
    await message.answer("📢 Barcha foydalanuvchilarga yubormoqchi bo'lgan xabaringizni yuboring:")


@router.message(Command("add"), F.from_user.id == ADMIN_ID)
async def cmd_add_balance(message: Message):
    parts = message.text.split()
    if len(parts) < 3:
        return await message.answer("⚠️ Ishlatish: `/add <amount_uzs> <userid>`")

    try:
        amount = float(parts[1])
        target_id = int(parts[2])
    except ValueError:
        return await message.answer("❌ Noto'g'ri format! Miqdor va UserID raqam bo'lishi kerak.")

    await update_user_balance(target_id, amount)
    new_bal = await get_user_balance(target_id)
    await message.answer(f"✅ Foydalanuvchi (`{target_id}`) balansiga <code>{amount:,.2f} UZS</code> qo'shildi!\nYangi balansi: <code>{new_bal:,.2f} UZS</code>")


@router.callback_query(F.data == "noop")
async def cb_noop(call: CallbackQuery):
    await call.answer()


# ==================== WEB SERVER & RUNNER ====================
async def health_check(request):
    return web.json_response({
        "status": "online",
        "service": "SMM Reseller Bot (+70% Markup & Telegram Stars)",
        "cached_services": len(services_cache["data"])
    })


async def start_web_server():
    app = web.Application()
    app.router.add_get("/", health_check)
    app.router.add_get("/health", health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=PORT)
    await site.start()
    logger.info(f"Render HTTP Web Service listening on port {PORT}")


async def main():
    if not BOT_TOKEN or not SMM_API_KEY:
        logger.error("CRITICAL: BOT_TOKEN and SMM_API_KEY must be set!")

    await init_db()
    await start_web_server()
    await get_services(force=True)

    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    await bot.delete_webhook(drop_pending_updates=True)
    logger.info("Starting Telegram Bot Polling (aiogram 3.13.1)...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
