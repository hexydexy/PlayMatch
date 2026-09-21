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
