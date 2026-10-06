"""One-time backfill: parse eBay break_time_text into starts_at for standard formatting.
Run once via Render shell: python3 /srv/app/backfill_ebay_times.py
(Brian 2026-10-06: eBay times should match other platforms' format)
"""
import os
import re
os.environ["no_proxy"] = os.environ["NO_PROXY"] = "localhost,127.0.0.1"

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import psycopg2

TZ_MAP = {
    "PT": "US/Pacific", "PST": "US/Pacific", "PDT": "US/Pacific",
    "MT": "US/Mountain", "MST": "US/Mountain", "MDT": "US/Mountain",
    "CT": "US/Central", "CST": "US/Central", "CDT": "US/Central",
    "ET": "US/Eastern", "EST": "US/Eastern", "EDT": "US/Eastern",
}


def parse_break_time(text):
    if not text:
        return None
    m = re.match(
        r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)\s*([A-Z]{2,4})?",
        text.strip(),
        re.IGNORECASE,
    )
    if not m:
        return None
    hour, minute, ampm, tz_abbr = m.groups()
    hour, minute = int(hour), int(minute or 0)
    if ampm.lower() == "pm" and hour != 12:
        hour += 12
    if ampm.lower() == "am" and hour == 12:
        hour = 0
    tz = ZoneInfo(TZ_MAP.get((tz_abbr or "CT").upper(), "US/Central"))
    now = datetime.now(tz)
    dt = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if dt <= now:
        dt = dt + timedelta(days=1)
    return dt.isoformat()


def main():
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    cur = conn.cursor()
    cur.execute(
        "SELECT id, break_time_text FROM breaks "
        "WHERE source='ebay' AND break_time_text IS NOT NULL AND starts_at IS NULL"
    )
    rows = cur.fetchall()
    print(f"Found {len(rows)} eBay listings to backfill", flush=True)
    updated = 0
    for bid, btt in rows:
        parsed = parse_break_time(btt)
        if parsed:
            cur.execute("UPDATE breaks SET starts_at=%s WHERE id=%s", (parsed, bid))
            updated += 1
    conn.commit()
    print(f"Updated {updated} listings with parsed start times", flush=True)
    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
