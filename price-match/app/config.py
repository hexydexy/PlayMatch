import os

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./playmatch.db")
BASE_CURRENCY = os.getenv("BASE_CURRENCY", "USD")
HTTP_TIMEOUT = float(os.getenv("HTTP_TIMEOUT", "15"))
USER_AGENT = os.getenv("USER_AGENT", "PlayMatch/0.1 (personal price tracker)")
# Politeness delay between outbound store requests (seconds)
REQUEST_DELAY = float(os.getenv("REQUEST_DELAY", "1.0"))
# Protects /api/admin/*. If empty, admin endpoints are disabled entirely.
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")
# GOGDB dump cache (one ~60 MB archive, re-used until a newer day is available)
GOGDB_CACHE_DIR = os.getenv("GOGDB_CACHE_DIR", "./.cache")
GOGDB_BASE_URL = os.getenv("GOGDB_BASE_URL", "https://www.gogdb.org/backups_v3/products")
# Daily job time (UTC hour). GOGDB publishes its snapshot at ~00:02 UTC.
SCHEDULE_HOUR_UTC = int(os.getenv("SCHEDULE_HOUR_UTC", "6"))
CORS_ORIGINS = os.getenv("CORS_ORIGINS", "*").split(",")

# --- full Steam catalog -----------------------------------------------------
# Free Steam Web API key; needed to list every Steam game. Without it catalog sync is skipped.
STEAM_API_KEY = os.getenv("STEAM_API_KEY", "")
# Steam games priced per request (`python -m app.bulk --probe` finds the largest that works)
STEAM_BATCH_SIZE = int(os.getenv("STEAM_BATCH_SIZE", "50"))
# Store a snapshot only when the price changed, or when the last one is this many days old
SNAPSHOT_HEARTBEAT_DAYS = int(os.getenv("SNAPSHOT_HEARTBEAT_DAYS", "7"))
# Epic lookups triggered by opening a game
EPIC_ON_DEMAND_PER_MINUTE = int(os.getenv("EPIC_ON_DEMAND_PER_MINUTE", "30"))
EPIC_COOLDOWN_HOURS = int(os.getenv("EPIC_COOLDOWN_HOURS", "24"))
# Most GOG near-matches queued for manual review in one daily run
REVIEW_QUEUE_CAP_PER_RUN = int(os.getenv("REVIEW_QUEUE_CAP_PER_RUN", "200"))
