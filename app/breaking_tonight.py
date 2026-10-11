""""Breaking Tonight" social content generator (Brian 2026-10-11).

Daily flagship social post, automated from BreakAlley's own break data:
queries live breaks + upcoming breaks tonight, renders a 9:16 PNG card
(dark sportsbook brand) plus a text caption ready to paste into X.

Served via /admin/breaking-tonight (preview) and /admin/breaking-tonight.png.
"""

from datetime import datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

CT = ZoneInfo("America/Chicago")

BG = (10, 22, 40)          # brand dark navy
GREEN = (0, 230, 118)      # brand green #00e676
WHITE = (240, 244, 248)
MUTED = (148, 163, 184)
CARD_BG = (17, 34, 58)
GOLD = (250, 204, 21)

W, H = 1080, 1920


def get_tonight_breaks(conn, limit: int = 8):
    """Live breaks now + upcoming breaks starting before midnight CT tonight.

    Dedupes eBay grouped listings by group_key. Live first, then by start time.
    """
    now_ct = datetime.now(CT)
    midnight = datetime.combine(now_ct.date() + timedelta(days=1), dtime(0, 0), tzinfo=CT)
    rows = conn.execute(
        """SELECT breaker, title_raw, product_normalized, sport, sports, format,
                  starts_at, is_live, source, source_url, price, currency,
                  group_key, is_auction
           FROM breaks
           WHERE (is_live = TRUE
                  OR (starts_at IS NOT NULL AND starts_at >= %(now)s
                      AND starts_at < %(midnight)s))
           ORDER BY is_live DESC, starts_at NULLS FIRST
           LIMIT %(lim)s""",
        {"now": now_ct, "midnight": midnight, "lim": limit * 3},
    ).fetchall()

    seen, out = set(), []
    for r in rows:
        key = r.get("group_key") or r.get("source_url")
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
        if len(out) >= limit:
            break
    return out


def _fmt_time(starts_at, is_live: bool) -> str:
    if is_live:
        return "LIVE NOW"
    if not starts_at:
        return "TBD"
    try:
        dt = starts_at
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=ZoneInfo("UTC"))
        ct = dt.astimezone(CT)
        return ct.strftime("%-I:%M %p").upper()
    except Exception:
        return "TBD"


def _short_title(title: str, product: str | None, max_len: int = 64) -> str:
    t = (title or product or "").strip()
    if len(t) > max_len:
        t = t[: max_len - 1].rstrip() + "…"
    return t


def _fit(d, text: str, fnt, max_w: float) -> str:
    """Trim text (pixel-measured) so it never overflows its box. Brian 2026-10-11."""
    text = (text or "").strip()
    if d.textlength(text, font=fnt) <= max_w:
        return text
    while text and d.textlength(text + "…", font=fnt) > max_w:
        text = text[:-1]
    return text.rstrip() + "…"


def build_caption(breaks, date_str: str | None = None) -> str:
    """Text caption for X / Threads."""
    if date_str is None:
        date_str = datetime.now(CT).strftime("%-m/%-d")
    lines = [f"\U0001f534 BREAKING TONIGHT ({date_str})", ""]
    for b in breaks:
        t = _fmt_time(b.get("starts_at"), bool(b.get("is_live")))
        breaker = (b.get("breaker") or "Unknown").strip()
        title = _short_title(b.get("title_raw"), b.get("product_normalized"), 58)
        lines.append(f"\u2022 {t} \u2014 {breaker}: {title}")
    lines += ["", "Full schedule \U0001f449 breakalley.com"]
    return "\n".join(lines)


def render_card(breaks) -> bytes:
    """Render the 9:16 PNG card. Returns PNG bytes."""
    from PIL import Image, ImageDraw, ImageFont

    def font(size: int, bold: bool = True):
        from PIL import ImageFont
        path = ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
                if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            # Render's slim image may lack DejaVu until fonts-dejavu-core installs
            return ImageFont.load_default(size=size)

    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)

    # Header
    d.rectangle([0, 0, W, 300], fill=(6, 14, 28))
    d.ellipse([70, 108, 110, 148], fill=GREEN)  # live dot
    header_fnt = font(64)
    header = _fit(d, "BREAKING TONIGHT", header_fnt, W - 140 - 70)
    d.text((140, 92), header, font=header_fnt, fill=WHITE)
    date_str = datetime.now(CT).strftime("%A, %B %-d").upper()
    d.text((72, 190), date_str, font=font(34, bold=False), fill=MUTED)
    d.text((72, 240), "Every live box break, one place  \u2014  breakalley.com",
           font=font(28, bold=False), fill=MUTED)

    y = 340
    # Inner text must stay inside the rounded card: margins 48 + padding 32 each side.
    TEXT_W = W - 48 * 2 - 32 * 2
    for b in breaks:
        is_live = bool(b.get("is_live"))
        t = _fmt_time(b.get("starts_at"), is_live)
        breaker_fnt, title_fnt = font(32), font(26, bold=False)
        breaker = _fit(d, b.get("breaker") or "Unknown", breaker_fnt, TEXT_W)
        title = _fit(d, _short_title(b.get("title_raw"), b.get("product_normalized"), 90),
                     title_fnt, TEXT_W)
        sport = (b.get("sport") or "").strip().upper()
        sport_fnt = font(24, bold=False)
        sport = _fit(d, sport, sport_fnt, 220)

        # Card row
        d.rounded_rectangle([48, y, W - 48, y + 172], radius=24, fill=CARD_BG)
        # Time / live badge
        if is_live:
            d.ellipse([80, y + 32, 104, y + 56], fill=GREEN)
            d.text((118, y + 24), "LIVE NOW", font=font(30), fill=GREEN)
        else:
            d.text((80, y + 24), _fit(d, t, font(30), TEXT_W), font=font(30), fill=GOLD)
        # Breaker
        d.text((80, y + 64), breaker, font=breaker_fnt, fill=WHITE)
        # Title
        d.text((80, y + 106), title, font=title_fnt, fill=MUTED)
        # Sport tag (right-aligned inside the card)
        if sport:
            d.text((W - 48 - 32 - d.textlength(sport, font=sport_fnt), y + 24),
                   sport, font=sport_fnt, fill=MUTED)
        y += 200
        if y > H - 220:
            break

    # Footer
    d.text((72, H - 120), "BREAKALLEY", font=font(40), fill=WHITE)
    d.text((72 + d.textlength("BREAKALLEY ", font=font(40)), H - 120),
           "breakalley.com", font=font(40, bold=False), fill=GREEN)
    d.text((72, H - 70), "The Google Flights of box breaks",
           font=font(26, bold=False), fill=MUTED)

    import io
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
