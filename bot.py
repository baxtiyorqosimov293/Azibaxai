import os
import io
import base64
import sqlite3
import logging
from contextlib import closing

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
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

DB_PATH = os.getenv("DB_PATH", "bot.db")

FREE_CREDITS = int(os.getenv("FREE_CREDITS", "3"))

PACK_10_STARS = int(os.getenv("PACK_10_STARS", "50"))
PACK_50_STARS = int(os.getenv("PACK_50_STARS", "200"))

IMAGE_MODEL = os.getenv("IMAGE_MODEL", "gpt-image-1")
IMAGE_SIZE = os.getenv("IMAGE_SIZE", "1024x1024")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN не найден в .env")
if not OPENAI_API_KEY:
    raise RuntimeError("OPENAI_API_KEY не найден в .env")

logging.basicConfig(level=logging.INFO)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(bot)
client = OpenAI(api_key=OPENAI_API_KEY)


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
            SELECT user_id, username, first_name, credits, total_spent, total_generations, last_prompt
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
            "last_prompt": row[6],
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


def save_payment(user_id: int, payload: str, stars_amount: int, credits_added: int, telegram_payment_charge_id: str):
    with closing(get_conn()) as conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO payments (user_id, payload, stars_amount, credits_added, telegram_payment_charge_id)
            VALUES (?, ?, ?, ?, ?)
        """, (user_id, payload, stars_amount, credits_added, telegram_payment_charge_id))
        conn.commit()


def get_stats():
    with closing(get_conn()) as conn:
        cur = conn.cursor()

        cur.execute("SELECT COUNT(*) FROM users")
        users_count = cur.fetchone()[0]

        cur.execute("SELECT COALESCE(SUM(total_spent), 0) FROM users")
        stars_sum = cur.fetchone()[0]

        cur.execute("SELECT COALESCE(SUM(total_generations), 0) FROM users")
        gens_sum = cur.fetchone()[0]

        return {
            "users": users_count,
            "stars": stars_sum,
            "generations": gens_sum,
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
# OPENAI IMAGE
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
    user = get_user(message.from_user.id)

    text = (
        f"Привет, {message.from_user.first_name or 'друг'} 👋\n\n"
        f"У тебя {user['credits']} бесплатных генераций.\n"
        f"Просто напиши описание картинки.\n\n"
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
        f"Всего генераций: {stats['generations']}"
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
        f"Всего генераций: <b>{user['total_generations']}</b>",
        parse_mode="HTML"
    )


@dp.message_handler(lambda m: m.text == "💳 Купить")
async def buy_menu_handler(message: types.Message):
    ensure_user(message.from_user)
    await message.answer(
        "Выбери пакет генераций 👇",
        reply_markup=buy_keyboard()
    )


@dp.message_handler(lambda m: m.text == "ℹ️ Помощь")
async def help_handler(message: types.Message):
    await message.answer(
        "ℹ️ Как пользоваться ботом\n\n"
        "1. Напиши описание картинки\n"
        "2. Получи результат\n"
        "3. Нажми «Повторить», если нужен ещё вариант\n"
        "4. Если кредиты закончатся — купи пакет\n\n"
        "Команды:\n"
        "/start — запустить бота\n"
        "/stats — статистика для админа"
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


@dp.message_handler(lambda m: m.text == "🎨 Создать")
async def create_hint_handler(message: types.Message):
    await message.answer("Напиши описание картинки текстом 🎨")


@dp.message_handler(content_types=types.ContentType.TEXT)
async def generate_handler(message: types.Message):
    text = (message.text or "").strip()

    if not text:
        return

    if text.startswith("/"):
        return

    if text in {"🎨 Создать", "👤 Профиль", "💳 Купить", "🔄 Повторить", "ℹ️ Помощь"}:
        return

    ensure_user(message.from_user)
    user = get_user(message.from_user.id)

    if user["credits"] <= 0:
        await message.answer(
            "❌ Бесплатные генерации закончились.\nВыбери пакет 👇",
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
    executor.start_polling(dp, skip_updates=True)
