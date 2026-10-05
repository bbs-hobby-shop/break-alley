"""Central config. Secrets come ONLY from environment variables — never hardcoded,
never logged."""
import os

from dotenv import load_dotenv

load_dotenv()  # reads .env in the project root if present

EBAY_APP_ID: str = os.environ.get("EBAY_APP_ID", "")
EBAY_CERT_ID: str = os.environ.get("EBAY_CERT_ID", "")
EBAY_CAMPID: str = os.environ.get("EBAY_CAMPID", "")  # eBay Partner Network campaign id (optional)
DATABASE_URL: str = os.environ.get(
    "DATABASE_URL", "postgresql://breaks:breaks@localhost:5432/breaks"
)
YOUTUBE_API_KEY: str = os.environ.get("YOUTUBE_API_KEY", "")  # Phase 2
TWITCH_CLIENT_ID: str = os.environ.get("TWITCH_CLIENT_ID", "")
TWITCH_CLIENT_SECRET: str = os.environ.get("TWITCH_CLIENT_SECRET", "")


def ebay_configured() -> bool:
    return bool(EBAY_APP_ID and EBAY_CERT_ID)


def youtube_configured() -> bool:
    return bool(YOUTUBE_API_KEY)
