import logging
import time
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatAction
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters,
)
from config import TELEGRAM_BOT_TOKEN, DEFAULT_SESSION, DEFAULT_MODEL
from opencode_client import client

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("tg-bot")

user_sessions: dict[int, str] = {}
user_models: dict[int, str] = {}
_models_cache: dict = {"data": [], "ts": 0.0}
CACHE_TTL = 300


async def get_free_models() -> list[str]:
    now = time.time()
    if now - _models_cache["ts"] < CACHE_TTL and _models_cache["data"]:
        return _models_cache["data"]
    models = await client.fetch_free_zen_models()
    if models:
        _models_cache["data"] = models
        _models_cache["ts"] = now
    return models


def get_model(uid: int) -> str:
    return user_models.get(uid, DEFAULT_MODEL)


async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ok = await client.health()
    status = "✅ OpenCode reachable" if ok else "⚠️ OpenCode not reachable"
    await update.message.reply_text(
        f"OpenCode bot online.\n{status}\n\n"
        "/new — new session\n/sessions — list sessions\n/use <id> — switch\n"
        "/models — pick a free model\n/abort — stop run\n/status — health\n/help — commands"
    )


async def help_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "/start — welcome\n/help — this message\n/new — new session\n"
        "/sessions — list sessions\n/use <id> — switch session\n/models — free models\n"
        "/status — health\n/abort — stop run\n\nSend any text as a prompt."
    )


async def status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ok = await client.health()
    uid = update.effective_user.id
    sid = user_sessions.get(uid, DEFAULT_SESSION)
    await update.message.reply_text(
        f"OpenCode: {'up' if ok else 'down'}\nSession: `{sid}`\nModel: `{get_model(uid)}`",
        parse_mode="Markdown",
    )


async def new_session(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        sid = await client.create_session(title=f"tg-{update.effective_user.id}")
        user_sessions[update.effective_user.id] = sid
        await update.message.reply_text(f"New session: `{sid}`", parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"Failed: {e}")


async def list_sessions(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        sessions = await client.list_sessions()
        if not sessions:
            await update.message.reply_text("No sessions.")
            return
        lines = [f"`{s['id']}` — {s.get('title', '(untitled)')}" for s in sessions[:20]]
        await update.message.reply_text("Sessions:\n" + "\n".join(lines), parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"Failed: {e}")


async def use_session(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Usage: /use <session-id>")
        return
    user_sessions[update.effective_user.id] = ctx.args[0]
    await update.message.reply_text(f"Switched to `{ctx.args[0]}`", parse_mode="Markdown")


async def models_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    models = await get_free_models()
    if not models:
        await update.message.reply_text("Couldn't fetch free models from Zen.")
        return
    keyboard = [[InlineKeyboardButton(m.split("/", 1)[1], callback_data=f"model:{m}")] for m in models]
    current = get_model(update.effective_user.id)
    await update.message.reply_text(
        f"Free models ({len(models)}) — current: `{current}`",
        reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown",
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


async def abort(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    sid = user_sessions.get(update.effective_user.id)
    if not sid:
        await update.message.reply_text("No active session.")
        return
    try:
        await client.abort(sid)
        await update.message.reply_text("Aborted.")
    except Exception as e:
        await update.message.reply_text(f"Failed: {e}")


async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    text = update.message.text
    if not text:
        return
    sid = user_sessions.get(uid)
    if not sid:
        try:
            sid = await client.create_session(title=f"tg-{uid}")
            user_sessions[uid] = sid
        except Exception as e:
            await update.message.reply_text(f"Couldn't open session: {e}")
            return
    model = get_model(uid)
    msg = await update.message.reply_text(f"⏳ thinking… (`{model.split('/', 1)[1]}`)", parse_mode="Markdown")
    await update.message.chat.send_action(ChatAction.TYPING)
    buffer = ""
    last_edit = 0.0
    try:
        async for event in client.send_message(sid, text, model=model):
            etype = event.get("type", "")
            if etype == "message.part.updated":
                part = event.get("properties", {}).get("part", {})
                if part.get("type") == "text":
                    buffer = part.get("text", buffer)
                elif part.get("type") == "tool":
                    buffer += f"\n🔧 `{part.get('tool', 'tool')}`\n"
            elif etype == "message.part.delta":
                props = event.get("properties", {})
                if props.get("field") == "text":
                    buffer += props.get("delta", "")
            elif etype in ("session.idle", "session.error"):
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
    app.add_handler(CommandHandler("new", new_session))
    app.add_handler(CommandHandler("sessions", list_sessions))
    app.add_handler(CommandHandler("use", use_session))
    app.add_handler(CommandHandler("models", models_command))
    app.add_handler(CommandHandler("abort", abort))
    app.add_handler(CallbackQueryHandler(model_callback, pattern=r"^model:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    log.info("Bot starting…")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
