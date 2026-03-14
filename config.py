import os

import yaml
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN       = os.environ["TELEGRAM_BOT_TOKEN"]
API_BASE        = f"https://api.telegram.org/bot{BOT_TOKEN}"
ALLOWED_IDS     = set(int(x) for x in os.getenv("ALLOWED_USER_IDS", "").split(",") if x.strip())
MANAGERS_CONFIG = os.getenv("MANAGERS_CONFIG", "managers.yaml")
PORT            = int(os.getenv("PORT", 8001))
ALERT_CHAT_ID   = int(os.getenv("ALERT_CHAT_ID", "0")) or next(iter(ALLOWED_IDS), None)
CHECK_INTERVAL  = int(os.getenv("MANAGER_CHECK_INTERVAL", "300"))  # seconds

with open(MANAGERS_CONFIG, encoding="utf-8") as _f:
    MANAGERS: list[dict] = yaml.safe_load(_f).get("managers", [])
