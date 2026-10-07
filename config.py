import os

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENCODE_URL = os.getenv("OPENCODE_URL", "http://127.0.0.1:4096")
DEFAULT_SESSION = os.getenv("DEFAULT_SESSION", "default")
DEFAULT_MODEL = "opencode/big-pickle"

if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN missing")
