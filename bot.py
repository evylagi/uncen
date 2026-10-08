import logging
import time
import asyncio
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatAction, ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)
from config import TELEGRAM_BOT_TOKEN, DEFAULT_MODEL
from zen_client import client

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("tg-bot")

# ── Per-user state ────────────────────────────────────────────────
user_models: dict[int, str] = {}
user_history: dict[int, list[dict]] = {}
user_files: dict[int, dict] = {}  # {uid: {"name": str, "content": str}}

_models_cache: dict = {"data": [], "ts": 0.0}
CACHE_TTL = 300

CODE_EXTS = {
    ".py", ".js", ".ts", ".jsx", ".tsx", ".java", ".c", ".cpp", ".h", ".hpp",
    ".go", ".rs", ".rb", ".php", ".sh", ".bash", ".zsh", ".fish",
    ".yaml", ".yml", ".toml", ".ini", ".cfg", ".json", ".xml", ".html",
    ".css", ".scss", ".sql", ".md", ".txt", ".log", ".env", ".gitignore",
    ".csv", ".tsv", ".bat", ".ps1", ".vue", ".svelte", ".r", ".lua",
    ".pl", ".kt", ".swift", ".dart", ".scala", ".clj", ".ex", ".exs",
}

MAX_FILE_SIZE = 20 * 1024 * 1024
MAX_HISTORY = 20
STREAM_TIMEOUT = 120  # seconds


# ── Helpers ───────────────────────────────────────────────────────

def esc(text: str) -> str:
    return (
        text.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
    )


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


def get_file(uid: int) -> dict | None:
    return user_files.get(uid)


def reset_user(uid: int):
    user_history[uid] = []
    user_files.pop(uid, None)


# ── Commands ──────────────────────────────────────────────────────

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ok = await client.health()
    status = "✅ Zen proxy reachable" if ok else "⚠️ Zen proxy not reachable"
    await update.message.reply_text(
        f"OpenCode Zen bot online.\n{status}\n\n"
        "/new — fresh conversation\n"
        "/compact — compress context\n"
        "/undo — remove last exchange\n"
        "/models — pick a free model\n"
        "/clearfile — remove attached file\n"
        "/status — health & current state\n"
        "/help — all commands\n\n"
        "Send text as a prompt, or attach a code/text file."
    )


async def help_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "*OpenCode Zen Bot*\n\n"
        "*Conversation*\n"
        "/new — fresh conversation\n"
        "/compact — compress context\n"
        "/clear — alias for /new\n"
        "/undo — remove last exchange\n\n"
        "*Model*\n"
        "/models — browse free Zen models\n"
        "/status — health & current state\n\n"
        "*Files*\n"
        "Send a code/text file to attach it\n"
        "/clearfile — remove current attachment\n\n"
        "Send any text as a prompt.",
        parse_mode=ParseMode.MARKDOWN,
    )


async def status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ok = await client.health()
    uid = update.effective_user.id
    history = get_history(uid)
    f = get_file(uid)
    file_info = f"`{f['name']}`" if f else "none"
    await update.message.reply_text(
        f"Zen proxy: {'up' if ok else 'down'}\n"
        f"Model: `{get_model(uid)}`\n"
        f"Messages: {len(history)}\n"
        f"Attached: {file_info}",
        parse_mode=ParseMode.MARKDOWN,
    )


async def new_session(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    reset_user(update.effective_user.id)
    await update.message.reply_text("Started a fresh conversation.")


async def compact(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    reset_user(update.effective_user.id)
    await update.message.reply_text("Context compacted.")


async def clear_history(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    reset_user(update.effective_user.id)
    await update.message.reply_text("Conversation cleared.")


async def undo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    history = get_history(uid)
    if len(history) >= 2:
        user_history[uid] = history[:-2]
        await update.message.reply_text("Removed last exchange.")
    else:
        await update.message.reply_text("Nothing to undo.")


async def redo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Redo not supported via proxy.")


async def init_agents(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "To initialize an AGENTS.md file, create it manually in your project root.\n"
        "OpenCode reads it automatically for project-specific instructions."
    )


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
        parse_mode=ParseMode.MARKDOWN,
    )


async def model_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    if not data.startswith("model:"):
        return
    model = data.split(":", 1)[1]
    user_models[update.effective_user.id] = model
    await query.edit_message_text(f"Model set to `{model}`", parse_mode=ParseMode.MARKDOWN)


async def clear_file(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid in user_files:
        name = user_files[uid]["name"]
        del user_files[uid]
        await update.message.reply_text(f"Removed `{name}`", parse_mode=ParseMode.MARKDOWN)
    else:
        await update.message.reply_text("No file attached.")


# ── File handler ──────────────────────────────────────────────────

async def handle_file(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    doc = update.message.document
    if not doc:
        return

    fname = doc.file_name or "unnamed"
    ext = "." + fname.rsplit(".", 1)[-1].lower() if "." in fname else ""

    # Extension filter restored
    if ext not in CODE_EXTS:
        await update.message.reply_text(
            f"Unsupported file type: `{ext}`\n"
            f"Supported: common text and code files.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    if doc.file_size and doc.file_size > MAX_FILE_SIZE:
        await update.message.reply_text("File too large (Telegram limit: 20MB).")
        return

    try:
        tg_file = await doc.get_file()
        raw = await tg_file.download_as_bytearray()
    except Exception as e:
        await update.message.reply_text(f"Download failed: {e}")
        return

    content = None
    for enc in ("utf-8", "latin-1"):
        try:
            content = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue

    if content is None:
        await update.message.reply_text(
            f"`{fname}` can't be read as text (binary encoding).",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    user_files[uid] = {"name": fname, "content": content}

    lines = content.splitlines()
    preview = "\n".join(lines[:15])
    if len(lines) > 15:
        preview += f"\n... ({len(lines) - 15} more lines)"

    await update.message.reply_text(
        f"📎 Attached: `{fname}` ({len(lines)} lines, {len(content)} chars)\n\n"
        f"```\n{preview[:1500]}\n```\n\n"
        f"Send a prompt to analyze it. Content is prepended to your next message.",
        parse_mode=ParseMode.MARKDOWN,
    )


# ── Message handler ───────────────────────────────────────────────

async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    text = update.message.text
    if not text:
        return

    f = get_file(uid)
    if f:
        effective = f"```{f['name']}\n{f['content']}\n```\n\n{text}"
    else:
        effective = text

    model = get_model(uid)
    short_model = model.split("/", 1)[-1]
    history = get_history(uid)
    history.append({"role": "user", "content": effective})
    history = history[-MAX_HISTORY:]

    msg = await update.message.reply_text(
        f"⏳ thinking… (`{short_model}`)",
        parse_mode=ParseMode.MARKDOWN,
    )
    await update.message.chat.send_action(ChatAction.TYPING)

    thinking_text = ""
    answer_text = ""
    last_edit = 0.0
    got_anything = False

    def render() -> str:
        parts = []
        if thinking_text:
            parts.append(
                "💭 <b>Thinking…</b>\n"
                f"<blockquote expandable>{esc(thinking_text[:600])}</blockquote>"
            )
        if answer_text:
            parts.append(esc(answer_text))
        return "\n\n".join(parts)[:4000]

    try:
        # Wrap the stream in a timeout so it can't hang forever
        async def _stream():
            nonlocal thinking_text, answer_text, last_edit, got_anything
            async for chunk in client.stream_chat(history, model):
                choice = chunk.get("choices", [{}])[0]
                delta = choice.get("delta", {})

                reasoning = delta.get("reasoning_content") or delta.get("reasoning")
                if reasoning:
                    thinking_text += reasoning
                    got_anything = True

                content = delta.get("content")
                if content:
                    got_anything = True
                    if "<think>" in content or "</think>" in content:
                        cleaned = content.replace("<think>", "").replace("</think>", "")
                        thinking_text += cleaned
                    else:
                        answer_text += content

                if choice.get("finish_reason"):
                    break

                now = time.time()
                if now - last_edit > 1.2:
                    body = render()
                    if body:
                        try:
                            await msg.edit_text(body, parse_mode=ParseMode.HTML)
                            last_edit = now
                        except Exception:
                            pass

        await asyncio.wait_for(_stream(), timeout=STREAM_TIMEOUT)

    except asyncio.TimeoutError:
        await msg.edit_text(
            f"❌ Timed out after {STREAM_TIMEOUT}s. Check the Zen proxy is running."
        )
        return
    except Exception as e:
        log.exception("stream error")
        await msg.edit_text(f"❌ Error: {e}")
        return

    if got_anything:
        history.append({"role": "assistant", "content": answer_text or thinking_text})
        body = render()
        for i in range(0, len(body), 4000):
            chunk = body[i:i + 4000]
            if i == 0:
                try:
                    await msg.edit_text(chunk, parse_mode=ParseMode.HTML)
                except Exception:
                    await update.message.reply_text(chunk)
            else:
                await update.message.reply_text(chunk)
    else:
        await msg.edit_text("(no response — model returned empty)")


async def on_shutdown(app: Application):
    await client.close()


# ── Entrypoint ────────────────────────────────────────────────────

def main():
    app = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .post_shutdown(on_shutdown)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("new", new_session))
    app.add_handler(CommandHandler("compact", compact))
    app.add_handler(CommandHandler("clear", clear_history))
    app.add_handler(CommandHandler("undo", undo))
    app.add_handler(CommandHandler("redo", redo))
    app.add_handler(CommandHandler("init", init_agents))
    app.add_handler(CommandHandler("models", models_command))
    app.add_handler(CommandHandler("clearfile", clear_file))
    app.add_handler(CallbackQueryHandler(model_callback, pattern=r"^model:"))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_file))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    log.info("Bot starting…")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
