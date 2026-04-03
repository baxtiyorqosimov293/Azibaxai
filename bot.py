import os
import io
import base64
import sqlite3
import logging
import threading
from contextlib import closing

from flask import Flask
from dotenv import load_dotenv
from openai import OpenAI

from aiogram import Bot, Dispatcher, types
from aiogram.types import (
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    LabeledPrice,
    ReplyKeyboardMarkup,
    KeyboardButton,
)
from aiogram.utils import executor

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
BOT_USERNAME = os.getenv("BOT_USERNAME", "your_bot_username").replace("@", "")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

DB_PATH = os.getenv("DB_PATH", "bot.db")
FREE_CREDITS = int(os.getenv("FREE_CREDITS", "3"))
REF_BONUS = int(os.getenv("REF_BONUS", "3"))

PACK_10_STARS = int(os.getenv("PACK_10_STARS", "69"))
PACK_50_STARS = int(os.getenv("PACK_50_STARS", "250"))

IMAGE_MODEL = os.getenv("IMAGE_MODEL", "gpt-image-1")
IMAGE_SIZE = os.getenv("IMAGE_SIZE", "1024x1024")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN не найден")
if not OPENAI_API_KEY:
    raise RuntimeError("OPENAI_API_KEY не найден")

logging.basicConfig(level=logging.INFO)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(bot)
client = OpenAI(api_key=OPENAI_API_KEY)

# =========================
# FLASK FOR RENDER WEB SERVICE
# =========================

web_app = Flask(__name__)

@web_app.route("/")
def home():
    return "Bot is running"

def run_web():
    port = int(os.environ.get("PORT", 10000))
    web_app.run(host="0.0.0.0", port=port)


# =========================
# DATABASE
# =========================

def get_conn():
    return sqlite3.connect(DB_PATH)


def init_db():
    with closing(get_conn()) as conn:
        cur = conn.cursor()

        cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            credits INTEGER NOT NULL DEFAULT 0,
            total_spent INTEGER NOT NULL DEFAULT 0,
            total_generations INTEGER NOT NULL DEFAULT 0,
            ref_count INTEGER NOT NULL DEFAULT 0,
            referred_by INTEGER,
            referral_bonus_received INTEGER NOT NULL DEFAULT 0,
            last_prompt TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            payload TEXT NOT NULL,
            stars_amount INTEGER NOT NULL,
            credits_added INTEGER NOT NULL,
            telegram_payment_charge_id TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS generations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            prompt TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """)

        conn.commit()


def ensure_user(tg_user: types.User):
    with closing(get_conn()) as conn:
        cur = conn.cursor()

        cur.execute("SELECT user_id FROM users WHERE user_id = ?", (tg_user.id,))
        row = cur.fetchone()

        if row is None:
            cur.execute("""
                INSERT INTO users (user_id, username, first_name, credits)
                VALUES (?, ?, ?, ?)
            """, (
                tg_user.id,
                tg_user.username,
                tg_user.first_name,
                FREE_CREDITS
            ))
        else:
            cur.execute("""
                UPDATE users
                SET username = ?, first_name = ?, updated_at = CURRENT_TIMESTAMP
                WHERE user_id = ?
            """, (
                tg_user.username,
                tg_user.first_name,
                tg_user.id
            ))

        conn.commit()


def get_user(user_id: int):
    with closing(get_conn()) as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT
                user_id, username, first_name, credits, total_spent,
                total_generations, ref_count, referred_by,
                referral_bonus_received, last_prompt
            FROM users
            WHERE user_id = ?
        """, (user_id,))
        row = cur.fetchone()

        if not row:
            return None

        return {
            "user_id": row[0],
            "username": row[1],
            "first_name": row[2],
            "credits": row[3],
            "total_spent": row[4],
            "total_generations": row[5],
            "ref_count": row[6],
            "referred_by": row[7],
            "referral_bonus_received": row[8],
            "last_prompt": row[9],
        }


def add_credits(user_id: int, amount: int):
    with closing(get_conn()) as conn:
        cur = conn.cursor()
        cur.execute("""
            UPDATE users
            SET credits = credits + ?, updated_at = CURRENT_TIMESTAMP
            WHERE user_id = ?
        """, (amount, user_id))
        conn.commit()


def subtract_credit(user_id: int):
    with closing(get_conn()) as conn:
        cur = conn.cursor()
        cur.execute("""
            UPDATE users
            SET credits = credits - 1,
                total_generations = total_generations + 1,
                updated_at = CURRENT_TIMESTAMP
            WHERE user_id = ? AND credits > 0
        """, (user_id,))
        conn.commit()


def set_last_prompt(user_id: int, prompt: str):
    with closing(get_conn()) as conn:
        cur = conn.cursor()
        cur.execute("""
            UPDATE users
            SET last_prompt = ?, updated_at = CURRENT_TIMESTAMP
            WHERE user_id = ?
        """, (prompt, user_id))
        conn.commit()


def increase_total_spent(user_id: int, amount: int):
    with closing(get_conn()) as conn:
        cur = conn.cursor()
        cur.execute("""
            UPDATE users
            SET total_spent = total_spent + ?, updated_at = CURRENT_TIMESTAMP
            WHERE user_id = ?
        """, (amount, user_id))
        conn.commit()


def save_generation(user_id: int, prompt: str):
    with closing(get_conn()) as conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO generations (user_id, prompt)
            VALUES (?, ?)
        """, (user_id, prompt))
        conn.commit()


def get_last_generations(user_id: int, limit: int = 5):
    with closing(get_conn()) as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT prompt, created_at
            FROM generations
            WHERE user_id = ?
            ORDER BY id DESC
            LIMIT ?
        """, (user_id, limit))
        return cur.fetchall()


def save_payment(user_id: int, payload: str, stars_amount: int, credits_added: int, telegram_payment_charge_id: str):
    with closing(get_conn()) as conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO payments (user_id, payload, stars_amount, credits_added, telegram_payment_charge_id)
            VALUES (?, ?, ?, ?, ?)
        """, (user_id, payload, stars_amount, credits_added, telegram_payment_charge_id))
        conn.commit()


def apply_referral(new_user_id: int, referrer_id: int):
    if new_user_id == referrer_id:
        return False, "Нельзя пригласить самого себя."

    with closing(get_conn()) as conn:
        cur = conn.cursor()

        cur.execute("""
            SELECT referred_by, referral_bonus_received
            FROM users
            WHERE user_id = ?
        """, (new_user_id,))
        row = cur.fetchone()

        if not row:
            return False, "Пользователь не найден."

        referred_by, referral_bonus_received = row

        if referred_by is not None or referral_bonus_received:
            return False, "Реферальный бонус уже использован."

        cur.execute("SELECT user_id FROM users WHERE user_id = ?", (referrer_id,))
        ref_exists = cur.fetchone()
        if not ref_exists:
            return False, "Реферер не найден."

        cur.execute("""
            UPDATE users
            SET referred_by = ?, referral_bonus_received = 1, credits = credits + ?, updated_at = CURRENT_TIMESTAMP
            WHERE user_id = ?
        """, (referrer_id, REF_BONUS, new_user_id))

        cur.execute("""
            UPDATE users
            SET credits = credits + ?, ref_count = ref_count + 1, updated_at = CURRENT_TIMESTAMP
            WHERE user_id = ?
        """, (REF_BONUS, referrer_id))

        conn.commit()
        return True, "Реферальный бонус начислен."


def get_stats():
    with closing(get_conn()) as conn:
        cur = conn.cursor()

        cur.execute("SELECT COUNT(*) FROM users")
        users_count = cur.fetchone()[0]

        cur.execute("SELECT COALESCE(SUM(total_spent), 0) FROM users")
        stars_sum = cur.fetchone()[0]

        cur.execute("SELECT COALESCE(SUM(total_generations), 0) FROM users")
        gens_sum = cur.fetchone()[0]

        cur.execute("SELECT COALESCE(SUM(ref_count), 0) FROM users")
        refs_sum = cur.fetchone()[0]

        return {
            "users": users_count,
            "stars": stars_sum,
            "generations": gens_sum,
            "refs": refs_sum,
        }


# =========================
# UI
# =========================

def main_menu():
    kb = ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add(
        KeyboardButton("🎨 Создать"),
        KeyboardButton("👤 Профиль"),
    )
    kb.add(
        KeyboardButton("💳 Купить"),
        KeyboardButton("🕘 История"),
    )
    kb.add(
        KeyboardButton("👥 Пригласить"),
        KeyboardButton("🔄 Повторить"),
    )
    kb.add(
        KeyboardButton("ℹ️ Помощь"),
    )
    return kb


def buy_keyboard():
    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(
        InlineKeyboardButton(f"⭐ 10 генераций — {PACK_10_STARS} Stars", callback_data="buy_10"),
        InlineKeyboardButton(f"⭐ 50 генераций — {PACK_50_STARS} Stars", callback_data="buy_50"),
    )
    return kb


def after_gen_keyboard():
    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(
        InlineKeyboardButton("🔄 Повторить", callback_data="regen"),
        InlineKeyboardButton("💳 Купить генерации", callback_data="open_buy"),
    )
    return kb


# =========================
# IMAGE GENERATION
# =========================

def generate_image_bytes(prompt: str) -> bytes:
    result = client.images.generate(
        model=IMAGE_MODEL,
        prompt=prompt,
        size=IMAGE_SIZE,
    )
    image_b64 = result.data[0].b64_json
    return base64.b64decode(image_b64)


# =========================
# HANDLERS
# =========================

@dp.message_handler(commands=["start"])
async def cmd_start(message: types.Message):
    ensure_user(message.from_user)
    parts = (message.get_args() or "").strip()

    if parts.isdigit():
        referrer_id = int(parts)
        ok, msg = apply_referral(message.from_user.id, referrer_id)
        if ok:
            await message.answer(f"🎁 {msg}\nТебе и другу начислено по {REF_BONUS} генерации.")
        else:
            if "уже использован" not in msg and "самого себя" not in msg:
                await message.answer(msg)

    user = get_user(message.from_user.id)

    text = (
        f"Привет, {message.from_user.first_name or 'друг'} 👋\n\n"
        f"У тебя {user['credits']} генераций.\n"
        f"Напиши описание картинки или нажми кнопку ниже.\n\n"
        f"Пример:\n"
        f"<i>Девушка в красном платье, студийный портрет</i>"
    )

    await message.answer(text, reply_markup=main_menu(), parse_mode="HTML")


@dp.message_handler(commands=["stats"])
async def cmd_stats(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return

    stats = get_stats()
    await message.answer(
        "📊 Статистика\n\n"
        f"Пользователей: {stats['users']}\n"
        f"Всего Stars: {stats['stars']}\n"
        f"Всего генераций: {stats['generations']}\n"
        f"Всего рефералов: {stats['refs']}"
    )


@dp.message_handler(lambda m: m.text == "👤 Профиль")
async def profile_handler(message: types.Message):
    ensure_user(message.from_user)
    user = get_user(message.from_user.id)

    await message.answer(
        "👤 Профиль\n\n"
        f"ID: <code>{user['user_id']}</code>\n"
        f"Кредиты: <b>{user['credits']}</b>\n"
        f"Всего потратил: <b>{user['total_spent']} Stars</b>\n"
        f"Всего генераций: <b>{user['total_generations']}</b>\n"
        f"Приглашено друзей: <b>{user['ref_count']}</b>",
        parse_mode="HTML"
    )


@dp.message_handler(lambda m: m.text == "💳 Купить")
async def buy_menu_handler(message: types.Message):
    ensure_user(message.from_user)
    await message.answer("Выбери пакет генераций 👇", reply_markup=buy_keyboard())


@dp.message_handler(lambda m: m.text == "ℹ️ Помощь")
async def help_handler(message: types.Message):
    await message.answer(
        "ℹ️ Как пользоваться ботом\n\n"
        "1. Нажми «Создать» или просто напиши промпт\n"
        "2. Получи картинку\n"
        "3. Нажми «Повторить», если нужен ещё вариант\n"
        "4. Если кредиты закончатся — купи пакет\n"
        "5. Приглашай друзей и получай бонусы\n\n"
        "Команды:\n"
        "/start — запустить бота\n"
        "/stats — статистика для админа"
    )


@dp.message_handler(lambda m: m.text == "🎨 Создать")
async def create_hint_handler(message: types.Message):
    await message.answer("Напиши описание картинки текстом 🎨")


@dp.message_handler(lambda m: m.text == "🕘 История")
async def history_handler(message: types.Message):
    ensure_user(message.from_user)
    rows = get_last_generations(message.from_user.id, limit=5)

    if not rows:
        await message.answer("История пока пустая.")
        return

    text = "🕘 Последние запросы:\n\n"
    for i, (prompt, created_at) in enumerate(rows, start=1):
        short_prompt = prompt if len(prompt) <= 80 else prompt[:80] + "..."
        text += f"{i}. {short_prompt}\n"

    await message.answer(text)


@dp.message_handler(lambda m: m.text == "👥 Пригласить")
async def referral_handler(message: types.Message):
    ensure_user(message.from_user)
    user_id = message.from_user.id

    if not BOT_USERNAME or BOT_USERNAME == "your_bot_username":
        await message.answer("Укажи BOT_USERNAME в переменных окружения.")
        return

    referral_link = f"https://t.me/{BOT_USERNAME}?start={user_id}"

    await message.answer(
        "👥 Приглашай друзей и получай бонусы\n\n"
        f"За каждого друга: +{REF_BONUS} генерации тебе и ему.\n\n"
        f"Твоя ссылка:\n{referral_link}"
    )


@dp.message_handler(lambda m: m.text == "🔄 Повторить")
async def repeat_from_menu_handler(message: types.Message):
    ensure_user(message.from_user)
    user = get_user(message.from_user.id)

    if user["credits"] <= 0:
        await message.answer("❌ У тебя нет генераций", reply_markup=buy_keyboard())
        return

    if not user["last_prompt"]:
        await message.answer("Нет предыдущего запроса для повторной генерации.")
        return

    await message.answer("⏳ Генерирую повторно...")

    try:
        image_bytes = generate_image_bytes(user["last_prompt"])
        subtract_credit(message.from_user.id)
        save_generation(message.from_user.id, user["last_prompt"])
        updated_user = get_user(message.from_user.id)

        photo = io.BytesIO(image_bytes)
        photo.name = "image.png"

        await message.answer_photo(
            photo=photo,
            caption=f"Готово ✅\nОсталось генераций: {updated_user['credits']}",
            reply_markup=after_gen_keyboard()
        )

    except Exception as e:
        logging.exception("Repeat generation failed")
        await message.answer(f"Ошибка генерации: {e}")


@dp.callback_query_handler(lambda c: c.data == "regen")
async def regen_callback(call: types.CallbackQuery):
    ensure_user(call.from_user)
    user = get_user(call.from_user.id)

    if user["credits"] <= 0:
        await call.message.answer("❌ У тебя нет генераций", reply_markup=buy_keyboard())
        await call.answer()
        return

    if not user["last_prompt"]:
        await call.message.answer("Нет предыдущего запроса для повторной генерации.")
        await call.answer()
        return

    await call.message.answer("⏳ Генерирую повторно...")

    try:
        image_bytes = generate_image_bytes(user["last_prompt"])
        subtract_credit(call.from_user.id)
        save_generation(call.from_user.id, user["last_prompt"])
        updated_user = get_user(call.from_user.id)

        photo = io.BytesIO(image_bytes)
        photo.name = "image.png"

        await call.message.answer_photo(
            photo=photo,
            caption=f"Готово ✅\nОсталось генераций: {updated_user['credits']}",
            reply_markup=after_gen_keyboard()
        )
        await call.answer()

    except Exception as e:
        logging.exception("Regen callback failed")
        await call.message.answer(f"Ошибка генерации: {e}")
        await call.answer()


@dp.callback_query_handler(lambda c: c.data == "open_buy")
async def open_buy_callback(call: types.CallbackQuery):
    await call.message.answer("Выбери пакет генераций 👇", reply_markup=buy_keyboard())
    await call.answer()


@dp.callback_query_handler(lambda c: c.data.startswith("buy_"))
async def buy_callback(call: types.CallbackQuery):
    if call.data == "buy_10":
        title = "10 генераций"
        amount = PACK_10_STARS
        credits = 10
    else:
        title = "50 генераций"
        amount = PACK_50_STARS
        credits = 50

    prices = [LabeledPrice(label=title, amount=amount)]

    await bot.send_invoice(
        chat_id=call.from_user.id,
        title=title,
        description="Покупка генераций",
        payload=f"credits_{credits}",
        currency="XTR",
        prices=prices,
    )
    await call.answer()


@dp.pre_checkout_query_handler(lambda q: True)
async def process_pre_checkout_query(pre_checkout_q: types.PreCheckoutQuery):
    await bot.answer_pre_checkout_query(pre_checkout_q.id, ok=True)


@dp.message_handler(content_types=types.ContentType.SUCCESSFUL_PAYMENT)
async def successful_payment_handler(message: types.Message):
    ensure_user(message.from_user)

    payload = message.successful_payment.invoice_payload
    total_amount = message.successful_payment.total_amount
    charge_id = message.successful_payment.telegram_payment_charge_id

    credits = int(payload.split("_")[1])

    add_credits(message.from_user.id, credits)
    increase_total_spent(message.from_user.id, total_amount)
    save_payment(
        user_id=message.from_user.id,
        payload=payload,
        stars_amount=total_amount,
        credits_added=credits,
        telegram_payment_charge_id=charge_id,
    )

    user = get_user(message.from_user.id)

    await message.answer(
        "✅ Оплата прошла успешно\n\n"
        f"Начислено генераций: {credits}\n"
        f"Текущий баланс: {user['credits']}",
        reply_markup=main_menu()
    )


@dp.message_handler(content_types=types.ContentType.TEXT)
async def generate_handler(message: types.Message):
    text = (message.text or "").strip()

    if not text:
        return

    if text.startswith("/"):
        return

    if text in {"🎨 Создать", "👤 Профиль", "💳 Купить", "🕘 История", "👥 Пригласить", "🔄 Повторить", "ℹ️ Помощь"}:
        return

    ensure_user(message.from_user)
    user = get_user(message.from_user.id)

    if user["credits"] <= 0:
        await message.answer(
            "❌ Генерации закончились.\nВыбери пакет 👇",
            reply_markup=buy_keyboard()
        )
        return

    await message.answer("⏳ Генерирую...")

    try:
        image_bytes = generate_image_bytes(text)
        subtract_credit(message.from_user.id)
        set_last_prompt(message.from_user.id, text)
        save_generation(message.from_user.id, text)

        updated_user = get_user(message.from_user.id)

        photo = io.BytesIO(image_bytes)
        photo.name = "image.png"

        await message.answer_photo(
            photo=photo,
            caption=f"Готово ✅\nОсталось генераций: {updated_user['credits']}",
            reply_markup=after_gen_keyboard()
        )

    except Exception as e:
        logging.exception("Generation failed")
        await message.answer(f"Ошибка генерации: {e}")


if __name__ == "__main__":
    init_db()
    threading.Thread(target=run_web).start()
    executor.start_polling(dp, skip_updates=True)
