import os
import base64
import logging
from openai import OpenAI
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes

logging.basicConfig(level=logging.INFO)

BOT_TOKEN = os.getenv("BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

client = OpenAI(api_key=OPENAI_API_KEY)

users = {}

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id

    if user_id not in users:
        users[user_id] = 3

    await update.message.reply_text(
        f"Привет!\nУ тебя {users[user_id]} бесплатных генераций\n\nНапиши описание картинки"
    )

async def generate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id

    if users.get(user_id, 0) <= 0:
        await update.message.reply_text("Нет кредитов")
        return

    prompt = update.message.text

    await update.message.reply_text("Генерирую...")

    try:
        result = client.images.generate(
            model="gpt-image-1",
            prompt=prompt,
            size="1024x1024"
        )

        image_base64 = result.data[0].b64_json
        image_bytes = base64.b64decode(image_base64)

        users[user_id] -= 1

        await update.message.reply_photo(
            photo=image_bytes,
            caption=f"Готово. Осталось генераций: {users[user_id]}"
        )

    except Exception as e:
        logging.exception("Generation error")
        await update.message.reply_text(f"Ошибка генерации: {e}")

app = ApplicationBuilder().token(BOT_TOKEN).build()

app.add_handler(CommandHandler("start", start))
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, generate))

app.run_polling()
