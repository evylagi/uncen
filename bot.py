import asyncio
import logging
import time
from pathlib import Path

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

# ── Behaviors file ────────────────────────────────────────────────
BEHAVIORS_FILE = Path("behaviors.txt")
_behaviors_state = {"enabled": False}

# ── Per-user state ────────────────────────────────────────────────
user_models: dict[int, str] = {}
user_history: dict[int, list[dict]] = {}
user_files: dict[int, dict] = {}

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
STREAM_TIMEOUT = 120

SYSTEM_TAG_OPEN = "[[SYSTEM]]"
SYSTEM_TAG_CLOSE = "[[/SYSTEM]]"


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


async def keep_typing(chat, stop_event: asyncio.Event):
    try:
        while not stop_event.is_set():
            try:
                await chat.send_action(ChatAction.TYPING)
            except Exception:
                pass
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=4.0)
            except asyncio.TimeoutError:
                continue
    except asyncio.CancelledError:
        pass


# ── Behaviors ─────────────────────────────────────────────────────

def behaviors_enabled() -> bool:
    return _behaviors_state["enabled"]


def behaviors_file_exists() -> bool:
    return BEHAVIORS_FILE.exists() and BEHAVIORS_FILE.is_file()


def read_behaviors() -> str | None:
    if not behaviors_enabled():
        return None
    if not behaviors_file_exists():
        return None
    try:
        return BEHAVIORS_FILE.read_text(encoding="utf-8")
    except Exception as e:
        log.warning("Failed to read %s: %s", BEHAVIORS_FILE, e)
        return None


def _wrap_prelude(behaviors: str, user_content: str) -> str:
    return (
        SYSTEM_TAG_OPEN + "\n"
        + behaviors + "\n"
        + SYSTEM_TAG_CLOSE + "\n\n"
        + user_content
    )


def build_payload(history: list[dict]) -> list[dict]:
    """
    Dual injection — system role + prelude tag on a user turn.

    The prelude is tagged on the FIRST user turn if untagged. If the first
    user turn has already been truncated out of history (or was never the
    tagged one), the CURRENT user turn gets the tag instead. Guarantees
    exactly one prelude per request, regardless of history length.
    """
    b = read_behaviors()
    if not b:
        return list(history)

    if not history:
        return [{"role": "system", "content": b}]

    # Copy so we don't mutate the caller's history
    working = [dict(m) for m in history]

    # Find the current (last) user message and check if any user message
    # in history already carries the prelude tag.
    already_tagged = any(
        m.get("role") == "user" and SYSTEM_TAG_OPEN in m.get("content", "")
        for m in working
    )

    if not already_tagged:
        # Tag the current user turn — survives truncation, always present.
        for m in reversed(working):
            if m.get("role") == "user":
                m["content"] = _wrap_prelude(b, m.get("content", ""))
                break

    return [{"role": "system", "content": b}, *working]


# ── Wrapper voice (placeholder only) ──────────────────────────────

def v_thinking(short_model: str) -> str:
    if behaviors_enabled():
        return f"`P mode · {short_model}`"
    return f"⏳ thinking… (`{short_model}`)"


def v_timeout() -> str:
    return f"❌ Timed out after {STREAM_TIMEOUT}s. The proxy may be down or the model is rate-limited."


def v_error(e: Exception) -> str:
    return f"❌ Error: {e}"


def v_model_error(msg: str) -> str:
    return f"❌ Model error: {msg}"


def v_empty() -> str:
    return (
        "⚠️ No response from model.\n"
        "This usually means the free daily quota for this model is exhausted.\n"
        "Try `/models` to switch to another free model."
    )


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
        "/behaviors — manage system instructions\n"
        "/status — health & current state\n"
        "/debug — dump current payload\n"
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
        "*Behaviors*\n"
        "/behaviors — status\n"
        "/behaviors on — enable system prompt from behaviors.txt\n"
        "/behaviors off — disable\n"
        "/behaviors show — dump current file\n"
        "/behaviors reload — re-check file\n\n"
        "*Debug*\n"
        "/debug — dump exact payload sent to zen\n\n"
        "Send any text as a prompt.",
        parse_mode=ParseMode.MARKDOWN,
    )


async def status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ok = await client.health()
    uid = update.effective_user.id
    history = get_history(uid)
    f = get_file(uid)
    file_info = f"`{f['name']}`" if f else "none"
    behav = (
        f"{'ON' if behaviors_enabled() else 'OFF'} "
        f"({'file ok' if behaviors_file_exists() else 'no file'})"
    )
    await update.message.reply_text(
        f"Zen proxy: {'up' if ok else 'down'}\n"
        f"Model: `{get_model(uid)}`\n"
        f"Messages: {len(history)}\n"
        f"Attached: {file_info}\n"
        f"Behaviors: {behav}",
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


async def behaviors_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    args = ctx.args or []
    sub = args[0].lower() if args else "status"

    if sub == "status":
        exists = behaviors_file_exists()
        enabled = behaviors_enabled()
        size = BEHAVIORS_FILE.stat().st_size if exists else 0
        preview = ""
        if exists:
            try:
                head = BEHAVIORS_FILE.read_text(encoding="utf-8")[:200].replace("`", "'")
                preview = f"\n\n```\n{head}…\n```"
            except Exception:
                pass
        await update.message.reply_text(
            f"*Behaviors*\n"
            f"File: `{BEHAVIORS_FILE}` ({'found' if exists else 'missing'})\n"
            f"Size: {size} bytes\n"
            f"State: *{'ON' if enabled else 'OFF'}*{preview}\n\n"
            "`/behaviors on` — enable\n"
            "`/behaviors off` — disable\n"
            "`/behaviors show` — dump current file\n"
            "`/behaviors reload` — re-check file\n"
            "`/debug` — dump payload sent to zen",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    if sub == "on":
        if not behaviors_file_exists():
            await update.message.reply_text(
                f"`{BEHAVIORS_FILE}` not found. Create it next to the bot, then retry.",
                parse_mode=ParseMode.MARKDOWN,
            )
            return
        _behaviors_state["enabled"] = True
        await update.message.reply_text(
            "Behaviors ON. System prompt + prelude injection active."
        )
        return

    if sub == "off":
        _behaviors_state["enabled"] = False
        await update.message.reply_text("Behaviors OFF.")
        return

    if sub == "reload":
        exists = behaviors_file_exists()
        size = BEHAVIORS_FILE.stat().st_size if exists else 0
        await update.message.reply_text(
            f"File {'found' if exists else 'missing'} — {size} bytes. "
            f"State: {'ON' if behaviors_enabled() else 'OFF'}.",
        )
        return

    if sub == "show":
        b = read_behaviors()
        if b is None:
            await update.message.reply_text("Behaviors are OFF or file missing.")
            return
        chunks = [b[i:i + 3500] for i in range(0, len(b), 3500)]
        for i, chunk in enumerate(chunks):
            header = f"*behaviors.txt* ({i + 1}/{len(chunks)})\n\n" if len(chunks) > 1 else ""
            await update.message.reply_text(
                header + f"```\n{chunk}\n```",
                parse_mode=ParseMode.MARKDOWN,
            )
        return

    await update.message.reply_text(
        "Usage:\n"
        "`/behaviors on` — enable\n"
        "`/behaviors off` — disable\n"
        "`/behaviors show` — dump current file\n"
        "`/behaviors reload` — re-check\n"
        "`/behaviors` — status\n"
        "`/debug` — dump payload",
        parse_mode=ParseMode.MARKDOWN,
    )


async def debug_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    history = get_history(uid)
    payload = build_payload(history)

    lines = [f"Payload — {len(payload)} message(s)"]
    for i, m in enumerate(payload):
        role = m.get("role", "?")
        content = m.get("content", "")
        head = content[:240].replace("\n", "⏎")
        tail = "…" if len(content) > 240 else ""
        lines.append(f"[{i}] {role} ({len(content)} chars)\n    {head}{tail}")

    body = "\n\n".join(lines)
    chunks = [body[i:i + 3500] for i in range(0, len(body), 3500)]
    for i, chunk in enumerate(chunks):
        header = f"*debug* ({i + 1}/{len(chunks)})\n\n" if len(chunks) > 1 else "*debug*\n\n"
        await update.message.reply_text(
            header + f"```\n{chunk}\n```",
            parse_mode=ParseMode.MARKDOWN,
        )


# ── File handler ──────────────────────────────────────────────────

async def handle_file(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    doc = update.message.document
    if not doc:
        return

    fname = doc.file_name or "unnamed"
    ext = "." + fname.rsplit(".", 1)[-1].lower() if "." in fname else ""

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
        f"Send a prompt to analyze it.",
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
    if len(history) > MAX_HISTORY:
        del history[:-MAX_HISTORY]

    msg = await update.message.reply_text(
        v_thinking(short_model),
        parse_mode=ParseMode.MARKDOWN,
    )

    thinking_text = ""
    answer_text = ""
    last_edit = 0.0
    last_body = ""
    got_anything = False
    error_text = ""

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

    stop_typing = asyncio.Event()
    typing_task = asyncio.create_task(keep_typing(update.message.chat, stop_typing))

    try:
        async def _stream():
            nonlocal thinking_text, answer_text, last_edit, last_body, got_anything, error_text
            payload = build_payload(history)
            log.info(
                "payload: %d msgs, roles=%s, first_user_tagged=%s",
                len(payload),
                [m.get("role") for m in payload],
                any(SYSTEM_TAG_OPEN in (m.get("content") or "")
                    for m in payload if m.get("role") == "user"),
            )
            async for chunk in client.stream_chat(payload, model):
                if isinstance(chunk, dict) and chunk.get("error"):
                    err = chunk["error"]
                    if isinstance(err, dict):
                        error_text = err.get("message", str(err))
                    else:
                        error_text = str(err)
                    return

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
                    if body and body != last_body:
                        try:
                            await msg.edit_text(body, parse_mode=ParseMode.HTML)
                            last_body = body
                            last_edit = now
                        except Exception:
                            pass

        await asyncio.wait_for(_stream(), timeout=STREAM_TIMEOUT)

    except asyncio.TimeoutError:
        stop_typing.set()
        typing_task.cancel()
        await msg.edit_text(v_timeout())
        return
    except Exception as e:
        stop_typing.set()
        typing_task.cancel()
        log.exception("stream error")
        await msg.edit_text(v_error(e))
        return
    finally:
        stop_typing.set()
        typing_task.cancel()

    if error_text:
        await msg.edit_text(v_model_error(error_text))
        return

    if got_anything:
        # Store the raw assistant answer (without tag pollution) in history
        history.append({"role": "assistant", "content": answer_text or thinking_text})
        body = render()
        if not body:
            await msg.edit_text("(empty response)")
            return
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
        await msg.edit_text(v_empty(), parse_mode=ParseMode.MARKDOWN)


async def on_shutdown(app: Application):
    await client.close()


# ── Entrypoint ────────────────────────────────────────────────────

def main():
    if behaviors_file_exists():
        log.info(
            "%s found (%d bytes) — behaviors default OFF, use /behaviors on",
            BEHAVIORS_FILE, BEHAVIORS_FILE.stat().st_size,
        )
    else:
        log.info("%s not found — behaviors unavailable until created", BEHAVIORS_FILE)

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
    app.add_handler(CommandHandler("behaviors", behaviors_command))
    app.add_handler(CommandHandler("debug", debug_command))
    app.add_handler(CallbackQueryHandler(model_callback, pattern=r"^model:"))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_file))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    log.info("Bot starting…")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
