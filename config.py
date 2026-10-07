import os

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

# zen-proxy default: http://127.0.0.1:8787/v1
ZEN_BASE_URL = os.getenv("ZEN_BASE_URL", "http://127.0.0.1:8787/v1")

# zen-proxy uses "public" as the key unless you set a custom proxyKey [citation:2]
ZEN_API_KEY = os.getenv("ZEN_API_KEY", "public")

DEFAULT_MODEL = os.getenv("DEFAULT_MODEL", "big-pickle")

if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN missing")
