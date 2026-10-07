import logging
import time
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatAction
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters,
)
from config import TELEGRAM_BOT_TOKEN, DEFAULT_MODEL
from zen_client import client

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("tg-bot")

# Per-user state
user_models: dict[int, str] = {}
user_history: dict[int, list[dict]] = {}  # simple conversation history

_models_cache: dict = {"data": [], "ts": 0.0}
CACHE_TTL = 300


async def get_free_models() -> list[str]:
    now = time.time()
    if now - _models_cache["ts"] < CACHE_TTL and _models_cache["data"]:
        return _models_cache["data"]
    models = await client.list_models()
    if models:
        _models_cache["data"] = models
        _models_cache["ts"] = now
    return models


def get_model(uid: int) -> str:
    return user_models.get(uid, DEFAULT_MODEL)


def get_history(uid: int) -> list[dict]:
    return user_history.setdefault(uid, [])


# ── Commands ──────────────────────────────────────────────────────

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ok = await client.health()
    status = "✅ Zen proxy reachable" if ok else "⚠️ Zen proxy not reachable"
    await update.message.reply_text(
        f"OpenCode Zen bot online.\n{status}\n\n"
        "/models — pick a free model\n"
        "/clear — reset conversation\n"
        "/status — health\n"
        "/help — commands"
    )


async def help_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "/start — welcome\n/help — this message\n"
        "/models — free Zen models\n/clear — reset chat history\n"
        "/status — health\n\nSend any text as a prompt."
    )


async def status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ok = await client.health()
    uid = update.effective_user.id
    await update.message.reply_text(
        f"Zen proxy: {'up' if ok else 'down'}\nModel: `{get_model(uid)}`",
        parse_mode="Markdown",
    )


async def clear_history(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_history[update.effective_user.id] = []
    await update.message.reply_text("Conversation cleared.")


async def models_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    models = await get_free_models()
    if not models:
        await update.message.reply_text("Couldn't fetch models from Zen proxy.")
        return
    keyboard = [
        [InlineKeyboardButton(m, callback_data=f"model:{m}")]
        for m in models
    ]
    current = get_model(update.effective_user.id)
    await update.message.reply_text(
        f"Free models — current: `{current}`",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown",
    )


async def model_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    if not data.startswith("model:"):
        return
    model = data.split(":", 1)[1]
    user_models[update.effective_user.id] = model
    await query.edit_message_text(f"Model set to `{model}`", parse_mode="Markdown")


# ── Message handler ───────────────────────────────────────────────

async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    text = update.message.text
    if not text:
        return

    model = get_model(uid)
    history = get_history(uid)
    history.append({"role": "user", "content": text})
    # Keep last 20 messages to bound context
    history = history[-20:]

    msg = await update.message.reply_text(
        f"⏳ thinking… (`{model}`)",
        parse_mode="Markdown",
    )
    await update.message.chat.send_action(ChatAction.TYPING)

    buffer = ""
    last_edit = 0.0

    try:
        async for chunk in client.stream_chat(history, model):
            choice = chunk.get("choices", [{}])[0]
            delta = choice.get("delta", {})
            content = delta.get("content")
            if content:
                buffer += content

            finish = choice.get("finish_reason")
            if finish:
                break

            now = time.time()
            if buffer and now - last_edit > 1.2:
                try:
                    await msg.edit_text(buffer[:4000])
                    last_edit = now
                except Exception:
                    pass
    except Exception as e:
        log.exception("stream error")
        await msg.edit_text(f"❌ Error: {e}")
        return

    if buffer:
        history.append({"role": "assistant", "content": buffer})
        for i in range(0, len(buffer), 4000):
            chunk = buffer[i:i + 4000]
            if i == 0:
                try:
                    await msg.edit_text(chunk)
                except Exception:
                    await update.message.reply_text(chunk)
            else:
                await update.message.reply_text(chunk)
    else:
        await msg.edit_text("(no response)")


async def on_shutdown(app: Application):
    await client.close()


def main():
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).post_shutdown(on_shutdown).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("clear", clear_history))
    app.add_handler(CommandHandler("models", models_command))
    app.add_handler(CallbackQueryHandler(model_callback, pattern=r"^model:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    log.info("Bot starting…")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
