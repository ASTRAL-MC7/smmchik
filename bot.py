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
# Exchange rate: How many UZS 1 Telegram Star gives the user in bot balance
UZS_PER_STAR = int(os.getenv("UZS_PER_STAR", "250"))
PORT = int(os.getenv("PORT", "8080"))
DB_PATH = "reseller_smm.db"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ==================== PREMIUM EMOJIS CONFIG ====================
# Note: Standard high-tier emojis are active out-of-the-box.
# Note on Telegram API: InlineKeyboardButton text only supports standard Unicode emojis.
# For message texts, if the bot owns a Fragment username or is operated carefully,
# you can replace these with <tg-emoji emoji-id="YOUR_ID">⚡</tg-emoji> tags.
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
    "categories": {},
    "by_id": {},
    "last_updated": 0
}
CACHE_TTL = 600  # 10 minutes cache as recommended by Kometa API docs


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
        cats = {}
        by_id = {}
        for s in res:
            # Apply +70% markup to base rate
            orig_rate = float(s.get("rate", 0))
            resell_rate = round(orig_rate * MARKUP, 2)
            s["reseller_rate"] = resell_rate

            sid = int(s["service"])
            cat_name = s.get("category", "Boshqa xizmatlar").strip()

            by_id[sid] = s
            cats.setdefault(cat_name, []).append(s)

        services_cache["data"] = res
        services_cache["categories"] = cats
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
        [
            InlineKeyboardButton(text=f"{E_CART} Xizmatlar (Katalog)", callback_data="cats:0")
        ],
        [
            InlineKeyboardButton(text=f"{E_BAG} Hisobim", callback_data="menu:balance"),
            InlineKeyboardButton(text=f"{E_STAR} Stars orqali to'ldirish", callback_data="menu:topup")
        ],
        [
            InlineKeyboardButton(text=f"{E_BOX} Buyurtmalarim", callback_data="orders:0"),
            InlineKeyboardButton(text=f"{E_DIAMOND} Yordam & Qoidalar", callback_data="menu:help")
        ]
    ]
    if is_admin:
        rows.append([InlineKeyboardButton(text="⚡️ Admin Panel", callback_data="admin:panel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def back_home_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"{E_BACK} Asosiy menyu", callback_data="menu:home")]
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
        f"{E_ROCKET} <b>Premium SMM Panel</b> botiga xush kelibsiz! "
        f"Ijtimoiy tarmoqlardagi sahifalaringizni tez, xavfsiz va avtomatik rivojlantiring.\n\n"
        f"{E_BAG} <b>Joriy balansingiz:</b> <code>{bal:,.2f} UZS</code>\n"
        f"{E_STAR} <b>To'lov tizimi:</b> Telegram Stars (24/7 Avto)\n\n"
        f"{E_GEAR} <i>Quyidagi tugmalar orqali kerakli bo'limni tanlang:</i>"
    )
    await message.answer(text, reply_markup=main_menu_kb(is_admin))


@router.callback_query(F.data == "menu:home")
async def cb_home(call: CallbackQuery, state: FSMContext):
    await state.clear()
    bal = await get_user_balance(call.from_user.id)
    is_admin = (call.from_user.id == ADMIN_ID)
    text = (
        f"{E_FIRE} <b>Asosiy Menyu</b>\n\n"
        f"{E_BAG} <b>Joriy balansingiz:</b> <code>{bal:,.2f} UZS</code>\n"
        f"{E_STAR} <b>Kurs:</b> 1 {E_STAR} = <code>{UZS_PER_STAR:,} UZS</code>\n\n"
        f"{E_GEAR} <i>Kerakli bo'limni tanlang:</i>"
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
        f"🆔 <b>Sizning ID:</b> <code>{call.from_user.id}</code>\n"
        f"{E_BAG} <b>Asosiy balans:</b> <code>{bal:,.2f} UZS</code>\n"
        f"📉 <b>Jami sarflangan:</b> <code>{spent:,.2f} UZS</code>\n"
        f"{E_BOX} <b>Jami buyurtmalar:</b> <code>{cnt} ta</code>\n\n"
        f"{E_STAR} <i>Hisobingizni Telegram Stars orqali bir zumda to'ldirishingiz mumkin!</i>"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"{E_STAR} Balansni to'ldirish", callback_data="menu:topup")],
        [InlineKeyboardButton(text=f"{E_BACK} Orqaga", callback_data="menu:home")]
    ])
    await safe_edit(call, text, kb)


@router.callback_query(F.data == "menu:help")
async def cb_help(call: CallbackQuery):
    text = (
        f"{E_DIAMOND} <b>Ma'lumot va Qoidalar</b>\n\n"
        f"1️⃣ <b>Buyurtma berish:</b> Xizmatni tanlang, ochiq profil yoki post havolasini yuboring va miqdorni kiriting.\n"
        f"2️⃣ <b>Kafolat:</b> Agar buyurtma ta'minotchi tomonidan bekor qilinsa (<i>Canceled</i>) yoki qisman bajarilsa (<i>Partial</i>), "
        f"mablag'ingiz <b>Buyurtmalarim</b> bo'limida tekshirilganda avtomatik balansingizga qaytariladi.\n"
        f"3️⃣ <b>To'lovlar:</b> Barcha to'lovlar rasmiy <b>Telegram Stars ({E_STAR})</b> orqali komissiyasiz va darhol qabul qilinadi.\n\n"
        f"{E_CLOCK} <b>Ish vaqti:</b> 24/7 Avtomatlashtirilgan tizim"
    )
    await safe_edit(call, text, back_home_kb())


# ==================== TELEGRAM STARS TOP-UP ====================
@router.callback_query(F.data == "menu:topup")
async def cb_topup(call: CallbackQuery, state: FSMContext):
    await state.clear()
    packages = [10, 25, 50, 100, 250, 500]
    rows = []
    row = []
    for stars in packages:
        uzs = stars * UZS_PER_STAR
        row.append(InlineKeyboardButton(
            text=f"{E_STAR} {stars} Stars ({uzs:,} UZS)",
            callback_data=f"buy_stars:{stars}"
        ))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)

    rows.append([InlineKeyboardButton(text=f"{E_HASH} Boshqa miqdor kiritish", callback_data="buy_stars:custom")])
    rows.append([InlineKeyboardButton(text=f"{E_BACK} Orqaga", callback_data="menu:home")])

    text = (
        f"{E_STAR} <b>Telegram Stars orqali hisobni to'ldirish</b>\n\n"
        f"{E_DIAMOND} <b>Konvertatsiya kursi:</b>\n"
        f"<code>1 Telegram Star = {UZS_PER_STAR:,} UZS</code>\n\n"
        f"{E_ROCKET} To'lov amalga oshirilishi bilan mablag' darhol balansingizga qo'shiladi. "
        f"Quyidagi paketlardan birini tanlang:"
    )
    await safe_edit(call, text, InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.startswith("buy_stars:"))
async def cb_buy_stars(call: CallbackQuery, state: FSMContext):
    val = call.data.split(":")[1]
    if val == "custom":
        await state.set_state(TopUpFlow.waiting_custom_stars)
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"{E_BACK} Bekor qilish", callback_data="menu:topup")]
        ])
        await safe_edit(
            call,
            f"{E_STAR} <b>Nechta Telegram Stars to'lamoqchisiz?</b>\n\n"
            f"<i>Masalan: 15 (Minimal: 1, Maksimal: 10000)</i>\n"
            f"1 {E_STAR} = <code>{UZS_PER_STAR:,} UZS</code>",
            kb
        )
        return

    stars = int(val)
    await send_stars_invoice(call.message, stars)
    await call.answer()


@router.message(TopUpFlow.waiting_custom_stars)
async def msg_custom_stars(message: Message, state: FSMContext):
    if not message.text or not message.text.isdigit():
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"{E_BACK} Bekor qilish", callback_data="menu:topup")]
        ])
        return await message.answer(f"{E_CROSS} Iltimos, faqat butun son kiriting!", reply_markup=kb)

    stars = int(message.text)
    if stars < 1 or stars > 10000:
        return await message.answer(f"{E_CROSS} Miqdor 1 va 10,000 Stars oralig'ida bo'lishi kerak!")

    await state.clear()
    await send_stars_invoice(message, stars)


async def send_stars_invoice(message: Message, stars: int):
    uzs_amount = stars * UZS_PER_STAR
    prices = [LabeledPrice(label=f"{uzs_amount:,} UZS balans", amount=stars)]
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"{E_STAR} To'lash ({stars} Stars)", pay=True)],
        [InlineKeyboardButton(text=f"{E_BACK} Asosiy menyu", callback_data="menu:home")]
    ])
    await message.answer_invoice(
        title="SMM Balansni To'ldirish",
        description=f"Hisobingizga {uzs_amount:,} UZS miqdorida mablag' qo'shiladi.",
        payload=f"topup:{stars}:{uzs_amount}",
        currency="XTR",  # Official Currency code for Telegram Stars
        prices=prices,
        provider_token="",  # Always empty for Telegram Stars
        reply_markup=kb
    )


@router.pre_checkout_query()
async def pre_checkout_handler(pre_checkout_query: PreCheckoutQuery):
    await pre_checkout_query.answer(ok=True)


@router.message(F.successful_payment)
async def successful_payment_handler(message: Message):
    payment = message.successful_payment
    payload = payment.invoice_payload
    if payload.startswith("topup:"):
        _, stars_str, uzs_str = payload.split(":")
        uzs_amount = float(uzs_str)
        await update_user_balance(message.from_user.id, uzs_amount)
        new_bal = await get_user_balance(message.from_user.id)

        await message.answer(
            f"{E_CHECK} <b>To'lov muvaffaqiyatli qabul qilindi!</b>\n\n"
            f"{E_STAR} <b>To'landi:</b> <code>{stars_str} Stars</code>\n"
            f"{E_BAG} <b>Qo'shildi:</b> <code>+{uzs_amount:,.2f} UZS</code>\n"
            f"{E_DIAMOND} <b>Yangi balansingiz:</b> <code>{new_bal:,.2f} UZS</code>",
            reply_markup=main_menu_kb(message.from_user.id == ADMIN_ID)
        )


# ==================== CATEGORIES & SERVICES CATALOG ====================
@router.callback_query(F.data.startswith("cats:"))
async def cb_categories(call: CallbackQuery, state: FSMContext):
    await state.clear()
    page = int(call.data.split(":")[1])
    cache = await get_services()
    cat_names = list(cache["categories"].keys())

    if not cat_names:
        return await safe_edit(
            call,
            f"{E_CROSS} <b>Xizmatlarni yuklashda xatolik!</b>\n"
            f"SMM API kaliti to'g'ri kiritilganligini tekshiring.",
            back_home_kb()
        )

    per_page = 8
    total_pages = (len(cat_names) + per_page - 1) // per_page
    start = page * per_page
    end = start + per_page
    current_cats = cat_names[start:end]

    rows = []
    for idx, c_name in enumerate(current_cats, start=start):
        count = len(cache["categories"][c_name])
        rows.append([InlineKeyboardButton(
            text=f"{E_FIRE} {c_name} ({count})",
            callback_data=f"cat:{idx}:0"
        )])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️ Oldingi", callback_data=f"cats:{page-1}"))
    nav.append(InlineKeyboardButton(text=f"📄 {page+1}/{total_pages}", callback_data="noop"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton(text="Keyingi ➡️", callback_data=f"cats:{page+1}"))
    if nav:
        rows.append(nav)

    rows.append([InlineKeyboardButton(text=f"{E_BACK} Asosiy menyu", callback_data="menu:home")])

    await safe_edit(
        call,
        f"{E_CART} <b>Kategoriyani tanlang:</b>\n"
        f"<i>Barcha xizmatlar avtomatik ravishda 24/7 ishlaydi.</i>",
        InlineKeyboardMarkup(inline_keyboard=rows)
    )


@router.callback_query(F.data.startswith("cat:"))
async def cb_category_services(call: CallbackQuery):
    _, cat_idx_str, page_str = call.data.split(":")
    cat_idx, page = int(cat_idx_str), int(page_str)

    cache = await get_services()
    cat_names = list(cache["categories"].keys())
    if cat_idx >= len(cat_names):
        return await call.answer("Kategoriya topilmadi!", show_alert=True)

    cat_name = cat_names[cat_idx]
    srv_list = cache["categories"][cat_name]

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
        btn_label = f"{E_GEAR} #{sid} {name[:26]} — {rate:,.0f} UZS"
        rows.append([InlineKeyboardButton(text=btn_label, callback_data=f"srv:{sid}:{cat_idx}:{page}")])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"cat:{cat_idx}:{page-1}"))
    nav.append(InlineKeyboardButton(text=f"📄 {page+1}/{total_pages}", callback_data="noop"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton(text="➡️", callback_data=f"cat:{cat_idx}:{page+1}"))
    if nav:
        rows.append(nav)

    rows.append([InlineKeyboardButton(text=f"{E_BACK} Kategoriyalarga qaytish", callback_data="cats:0")])

    text = (
        f"{E_FIRE} <b>Kategoriya:</b> {cat_name}\n\n"
        f"{E_DIAMOND} <i>Narxlar 1,000 dona uchun ko'rsatilgan. Xizmatni tanlang:</i>"
    )
    await safe_edit(call, text, InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.startswith("srv:"))
async def cb_service_detail(call: CallbackQuery, state: FSMContext):
    await state.clear()
    parts = call.data.split(":")
    sid = int(parts[1])
    cat_idx = int(parts[2]) if len(parts) > 2 else 0
    page = int(parts[3]) if len(parts) > 3 else 0

    cache = await get_services()
    s = cache["by_id"].get(sid)
    if not s:
        return await call.answer("Xizmat topilmadi!", show_alert=True)

    refill_txt = f"{E_CHECK} Mavjud" if s.get("refill") else f"{E_CROSS} Yo'q"
    cancel_txt = f"{E_CHECK} Mavjud" if s.get("cancel") else f"{E_CROSS} Yo'q"

    text = (
        f"{E_ROCKET} <b>Xizmat #{s['service']}: {s['name']}</b>\n\n"
        f"📂 <b>Kategoriya:</b> {s['category']}\n"
        f"{E_BAG} <b>Narxi (1000 ta uchun):</b> <code>{s['reseller_rate']:,.2f} UZS</code>\n"
        f"⬇️ <b>Minimal buyurtma:</b> <code>{s['min']} ta</code>\n"
        f"⬆️ <b>Maksimal buyurtma:</b> <code>{s['max']} ta</code>\n"
        f"♻️ <b>Qayta tiklash (Refill):</b> {refill_txt}\n"
        f"🚫 <b>Bekor qilish (Cancel):</b> {cancel_txt}\n"
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"{E_CHECK} Buyurtma berish", callback_data=f"order_start:{sid}")],
        [InlineKeyboardButton(text=f"{E_BACK} Orqaga", callback_data=f"cat:{cat_idx}:{page}")]
    ])
    await safe_edit(call, text, kb)


# ==================== ORDER PLACEMENT FLOW ====================
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
        [InlineKeyboardButton(text=f"{E_BACK} Bekor qilish", callback_data=f"srv:{sid}:0:0")]
    ])
    await safe_edit(
        call,
        f"{E_LINK} <b>Buyurtma uchun havolani (link) yuboring:</b>\n\n"
        f"📌 <b>Xizmat:</b> {s['name']}\n"
        f"⚠️ <i>Eslatma: Havola https:// bilan boshlanishi va profil/kanal ochiq bo'lishi shart!</i>",
        kb
    )


@router.message(OrderFlow.waiting_link)
async def msg_order_link(message: Message, state: FSMContext):
    link = (message.text or "").strip()
    data = await state.get_data()
    sid = data["service_id"]

    kb_cancel = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"{E_BACK} Bekor qilish", callback_data=f"srv:{sid}:0:0")]
    ])

    if not (link.startswith("http://") or link.startswith("https://")) or len(link) > 500:
        return await message.answer(
            f"{E_CROSS} <b>Noto'g'ri havola!</b>\n"
            f"Havola <code>https://</code> bilan boshlanishi va 500 belgidan oshmasligi kerak.",
            reply_markup=kb_cancel
        )

    cache = await get_services()
    s = cache["by_id"].get(sid)

    await state.update_data(link=link)
    await state.set_state(OrderFlow.waiting_quantity)

    await message.answer(
        f"{E_HASH} <b>Endi buyurtma miqdorini (sonini) kiriting:</b>\n\n"
        f"⬇️ <b>Minimal:</b> <code>{s['min']}</code>\n"
        f"⬆️ <b>Maksimal:</b> <code>{s['max']}</code>\n"
        f"{E_BAG} <b>Narx (1000 ta):</b> <code>{s['reseller_rate']:,.2f} UZS</code>",
        reply_markup=kb_cancel
    )


@router.message(OrderFlow.waiting_quantity)
async def msg_order_quantity(message: Message, state: FSMContext):
    data = await state.get_data()
    sid = data["service_id"]
    link = data["link"]

    kb_cancel = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"{E_BACK} Bekor qilish", callback_data=f"srv:{sid}:0:0")]
    ])

    if not message.text or not message.text.isdigit():
        return await message.answer(f"{E_CROSS} Iltimos, faqat raqam kiriting!", reply_markup=kb_cancel)

    qty = int(message.text)
    cache = await get_services()
    s = cache["by_id"].get(sid)
    min_q, max_q = int(s["min"]), int(s["max"])

    if qty < min_q or qty > max_q:
        return await message.answer(
            f"{E_CROSS} <b>Miqdor chegaradan tashqarida!</b>\n"
            f"Minimal <code>{min_q}</code> va maksimal <code>{max_q}</code> orasida son kiriting.",
            reply_markup=kb_cancel
        )

    total_charge = round((qty / 1000.0) * s["reseller_rate"], 2)
    bal = await get_user_balance(message.from_user.id)

    await state.update_data(quantity=qty, charge=total_charge)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"{E_CHECK} Tasdiqlash va Yuborish", callback_data="order_confirm")],
        [InlineKeyboardButton(text=f"{E_CROSS} Bekor qilish", callback_data="menu:home")]
    ])

    await message.answer(
        f"{E_DIAMOND} <b>Buyurtmani tasdiqlang:</b>\n\n"
        f"📌 <b>Xizmat:</b> #{sid} — {s['name']}\n"
        f"{E_LINK} <b>Havola:</b> <code>{link}</code>\n"
        f"{E_HASH} <b>Miqdor:</b> <code>{qty:,} ta</code>\n"
        f"{E_BAG} <b>Jami narx:</b> <code>{total_charge:,.2f} UZS</code>\n"
        f"💳 <b>Sizning balansingiz:</b> <code>{bal:,.2f} UZS</code>",
        reply_markup=kb
    )


@router.callback_query(F.data == "order_confirm")
async def cb_order_confirm(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if not data or "service_id" not in data:
        return await call.answer("Buyurtma ma'lumotlari eskirgan!", show_alert=True)

    sid = data["service_id"]
    link = data["link"]
    qty = data["quantity"]
    charge = data["charge"]
    await state.clear()

    bal = await get_user_balance(call.from_user.id)
    if bal < charge:
        needed = charge - bal
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"{E_STAR} Hisobni to'ldirish", callback_data="menu:topup")],
            [InlineKeyboardButton(text=f"{E_BACK} Asosiy menyu", callback_data="menu:home")]
        ])
        return await safe_edit(
            call,
            f"{E_CROSS} <b>Balansingizda mablag' yetarli emas!</b>\n\n"
            f"{E_BAG} Kerakli summa: <code>{charge:,.2f} UZS</code>\n"
            f"💳 Sizning balansingiz: <code>{bal:,.2f} UZS</code>\n"
            f"📉 Yetishmayotgan qism: <code>{needed:,.2f} UZS</code>",
            kb
        )

    # Deduct upfront before calling API
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
            [InlineKeyboardButton(text=f"{E_BOX} Buyurtmani kuzatish", callback_data=f"ord:{api_order_id}")],
            [InlineKeyboardButton(text=f"{E_BACK} Asosiy menyu", callback_data="menu:home")]
        ])
        await safe_edit(
            call,
            f"{E_CHECK} <b>Buyurtmangiz muvaffaqiyatli qabul qilindi!</b>\n\n"
            f"🆔 <b>Buyurtma ID:</b> <code>#{api_order_id}</code>\n"
            f"📌 <b>Xizmat:</b> {s_name}\n"
            f"{E_HASH} <b>Miqdor:</b> <code>{qty:,} ta</code>\n"
            f"{E_BAG} <b>Yechildi:</b> <code>{charge:,.2f} UZS</code>\n"
            f"💳 <b>Qolgan balans:</b> <code>{new_bal:,.2f} UZS</code>",
            kb
        )
    else:
        # Automatic refund if provider failed to place order
        await update_user_balance(call.from_user.id, charge)
        err_msg = res.get("error", "Noma'lum xatolik") if isinstance(res, dict) else "Server xatosi"
        await safe_edit(
            call,
            f"{E_CROSS} <b>Buyurtma yaratishda xatolik yuz berdi!</b>\n\n"
            f"📄 <b>Sabab:</b> <code>{err_msg}</code>\n"
            f"{E_CHECK} <i>Mablag'ingiz ({charge:,.2f} UZS) balansingizga to'liq qaytarildi.</i>",
            back_home_kb()
        )


# ==================== ORDERS HISTORY, STATUS, REFILL & CANCEL ====================
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
        return await safe_edit(
            call,
            f"{E_BOX} <b>Sizda hali buyurtmalar mavjud emas.</b>\n"
            f"Birinchi buyurtmangizni katalog orqali bering!",
            back_home_kb()
        )

    total_pages = (total + per_page - 1) // per_page
    kb_rows = []
    for api_id, s_name, qty, charge, status in rows_db:
        icon = E_CHECK if status == "Completed" else (E_CROSS if status in ("Canceled", "Refunded") else E_CLOCK)
        kb_rows.append([InlineKeyboardButton(
            text=f"{icon} #{api_id} | {s_name[:18]} ({qty}) — {status}",
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

    kb_rows.append([InlineKeyboardButton(text=f"{E_BACK} Asosiy menyu", callback_data="menu:home")])

    await safe_edit(
        call,
        f"{E_BOX} <b>Sizning buyurtmalaringiz ({total} ta):</b>\n"
        f"<i>Batafsil ma'lumot, holatni yangilash, Refill yoki Bekor qilish uchun buyurtmani tanlang:</i>",
        InlineKeyboardMarkup(inline_keyboard=kb_rows)
    )


@router.callback_query(F.data.startswith("ord:"))
async def cb_order_detail(call: CallbackQuery):
    api_order_id = int(call.data.split(":")[1])

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT service_id, service_name, link, quantity, charge, status FROM orders "
            "WHERE user_id = ? AND api_order_id = ?",
            (call.from_user.id, api_order_id)
        ) as cur:
            row = await cur.fetchone()

    if not row:
        return await call.answer("Buyurtma topilmadi!", show_alert=True)

    sid, s_name, link, qty, charge, db_status = row

    # Live status check from Kometa API
    api_res = await smm_request({"action": "status", "order": api_order_id})
    start_count = "-"
    remains = "-"
    live_status = db_status
    refund_note = ""

    if isinstance(api_res, dict) and "status" in api_res:
        live_status = api_res["status"]
        start_count = api_res.get("start_count", "-")
        remains = api_res.get("remains", "-")

        # Automatic refund handling if provider canceled or partially completed
        if live_status == "Canceled" and db_status not in ("Canceled", "Refunded"):
            await update_user_balance(call.from_user.id, charge)
            live_status = "Refunded"
            refund_note = f"\n\n{E_CHECK} <b>Buyurtma bekor qilingani sababli {charge:,.2f} UZS balansingizga qaytarildi!</b>"
        elif live_status == "Partial" and db_status not in ("Partial", "Refunded"):
            rem_int = int(remains) if str(remains).isdigit() else 0
            if 0 < rem_int <= qty:
                refund_amt = round((rem_int / qty) * charge, 2)
                await update_user_balance(call.from_user.id, refund_amt)
                refund_note = f"\n\n{E_CHECK} <b>Qisman bajarildi: yetkazilmagan {rem_int} ta uchun {refund_amt:,.2f} UZS qaytarildi!</b>"

        if live_status != db_status:
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute(
                    "UPDATE orders SET status = ? WHERE api_order_id = ?",
                    (live_status, api_order_id)
                )
                await db.commit()

    text = (
        f"{E_BOX} <b>Buyurtma tafsilotlari: #{api_order_id}</b>\n\n"
        f"📌 <b>Xizmat:</b> {s_name}\n"
        f"{E_LINK} <b>Havola:</b> <code>{link}</code>\n"
        f"{E_HASH} <b>Buyurtma miqdori:</b> <code>{qty:,} ta</code>\n"
        f"▶️ <b>Boshlang'ich soni:</b> <code>{start_count}</code>\n"
        f"⏳ <b>Qoldiq (Remains):</b> <code>{remains}</code>\n"
        f"{E_BAG} <b>To'langan summa:</b> <code>{charge:,.2f} UZS</code>\n"
        f"📊 <b>Holati:</b> <b>{live_status}</b>{refund_note}"
    )

    action_btns = [
        InlineKeyboardButton(text=f"{E_REFRESH} Yangilash", callback_data=f"ord:{api_order_id}")
    ]
    extra_btns = [
        InlineKeyboardButton(text="♻️ Refill (To'ldirish)", callback_data=f"act_refill:{api_order_id}"),
        InlineKeyboardButton(text="🚫 Cancel (Bekor qilish)", callback_data=f"act_cancel:{api_order_id}")
    ]

    kb = InlineKeyboardMarkup(inline_keyboard=[
        action_btns,
        extra_btns,
        [InlineKeyboardButton(text=f"{E_BACK} Buyurtmalar ro'yxati", callback_data="orders:0")]
    ])
    await safe_edit(call, text, kb)


@router.callback_query(F.data.startswith("act_refill:"))
async def cb_act_refill(call: CallbackQuery):
    api_order_id = int(call.data.split(":")[1])
    res = await smm_request({"action": "refill", "order": api_order_id})
    if isinstance(res, dict) and "error" not in res:
        await call.answer("✅ Refill (qayta to'ldirish) so'rovi yuborildi!", show_alert=True)
    else:
        err = res.get("error", "Refill bu xizmat uchun mavjud emas") if isinstance(res, dict) else "Xatolik"
        await call.answer(f"❌ {err}", show_alert=True)


@router.callback_query(F.data.startswith("act_cancel:"))
async def cb_act_cancel(call: CallbackQuery):
    api_order_id = int(call.data.split(":")[1])
    res = await smm_request({"action": "cancel", "orders": str(api_order_id)})
    if isinstance(res, dict) and "error" in res:
        await call.answer(f"❌ {res['error']}", show_alert=True)
    else:
        await call.answer(
            "✅ Bekor qilish so'rovi yuborildi! Birozdan so'ng 'Yangilash' tugmasini bosing.",
            show_alert=True
        )


# ==================== ADMIN PANEL & COMMANDS ====================
@router.callback_query(F.data == "admin:panel")
async def cb_admin_panel(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return await call.answer("Ruxsat berilmagan!", show_alert=True)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Statistika (/stats)", callback_data="admin:stats")],
        [InlineKeyboardButton(text="📦 So'nggi buyurtmalar (/orders)", callback_data="admin:orders")],
        [InlineKeyboardButton(text="📢 Xabar tarqatish (/broadcast)", callback_data="admin:broadcast")],
        [InlineKeyboardButton(text=f"{E_BACK} Asosiy menyu", callback_data="menu:home")]
    ])
    await safe_edit(call, "⚡️ <b>Admin Boshqaruv Paneli</b>\n\nKerakli bo'limni tanlang:", kb)


@router.callback_query(F.data == "admin:stats")
async def cb_admin_stats(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return await call.answer("Ruxsat berilmagan!", show_alert=True)

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
        return await call.answer("Ruxsat berilmagan!", show_alert=True)

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT api_order_id, user_id, service_name, quantity, status FROM orders ORDER BY id DESC LIMIT 10") as cur:
            rows = await cur.fetchall()

    if not rows:
        text = "📦 Hozircha buyurtmalar mavjud emas."
    else:
        text = "📦 <b>So'nggi 10 ta buyurtma:</b>\n\n"
        for r in rows:
            api_id, u_id, s_name, qty, status = r
            text += f"▪️ #{api_id} | User: <code>{u_id}</code> | {s_name[:12]} ({qty}) — <b>{status}</b>\n"

    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Orqaga", callback_data="admin:panel")]])
    await safe_edit(call, text, kb)


@router.callback_query(F.data == "admin:broadcast")
async def cb_admin_broadcast(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        return await call.answer("Ruxsat berilmagan!", show_alert=True)

    await state.set_state(BroadcastFlow.waiting_message)
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Bekor qilish", callback_data="admin:panel")]])
    await safe_edit(call, "📢 <b>Barcha foydalanuvchilarga yuboriladigan xabarni yuboring:</b>", kb)


@router.message(BroadcastFlow.waiting_message, F.from_user.id == ADMIN_ID)
async def process_broadcast(message: Message, state: FSMContext):
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

    if not rows:
        return await message.answer("📦 Buyurtmalar yo'q.")

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


# ==================== RENDER WEB SERVER + BOT RUNNER ====================
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
        logger.error("CRITICAL: BOT_TOKEN and SMM_API_KEY must be set in Environment Variables!")

    await init_db()
    await start_web_server()

    # Pre-warm the Kometa-SMM services cache on boot
    await get_services(force=True)

    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    await bot.delete_webhook(drop_pending_updates=True)
    logger.info("Starting Telegram Bot Polling...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
