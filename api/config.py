import os

RESTAURANT_NAME = "itailaew"
RESTAURANT_TAGLINE = "Italian Restaurant & Café"
VAT_RATE = 0.07
SERVICE_CHARGE_RATE = 0.10
POLL_INTERVAL_SECONDS = 10
# Two reservations for the same table closer than this many minutes are treated as a double booking
RESERVATION_SLOT_MINUTES = 90

FIREBASE_DB_URL = os.environ.get("FIREBASE_DB_URL", "").rstrip("/")
FIREBASE_API_KEY = os.environ.get("FIREBASE_API_KEY", "")
FIREBASE_DB_SECRET = os.environ.get("FIREBASE_DB_SECRET", "")
FIREBASE_STORAGE_BUCKET = os.environ.get("FIREBASE_STORAGE_BUCKET", "").strip()
BOOTSTRAP_SECRET = os.environ.get("BOOTSTRAP_SECRET", "")
LOCAL_AUTH_SECRET = os.environ.get("LOCAL_AUTH_SECRET", "itailaew-local-dev-secret-change-me")
LOCAL_DB_PATH = os.environ.get("LOCAL_DB_PATH", os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "database.json"))

ALLOWED_ROLES = {"admin", "staff", "customer"}


def is_configured():
    """Return whether production Firebase configuration is available."""
    return bool(FIREBASE_DB_URL and FIREBASE_API_KEY and FIREBASE_DB_SECRET)


def is_local_mode():
    """Local mode keeps the app runnable without external setup."""
    return not bool(FIREBASE_DB_URL and FIREBASE_API_KEY)
