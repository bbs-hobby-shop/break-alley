"""YouTube channel-roster ingestion: cheap per-channel monitoring.

Rationale: search.list costs 100 quota units/call, which caps discovery at
~16 searches/run. But once a breaker channel is known, checking its uploads
costs 1 unit (playlistItems.list) plus batched videos.list details
(1 unit per 50 ids). The search poller (app/youtube.py) discovers channels;
the roster poller watches them.

QUOTA MATH (free tier = 10,000 units/day):
  - playlistItems.list call = 1 unit  (recent uploads per channel)
  - videos.list call        = 1 unit  (up to 50 video ids per call)

  Worst case per run:  C channels x 1 playlist call
                       + ceil(C x VIDEOS_PER_CHANNEL / 50) detail calls
  Default (300 channels x 10 videos): 300 + ceil(3000/50) = 360 units/run.

  The uploads playlist id is derived deterministically ("UC.." -> "UU.."),
  so no channels.list call is needed to find it.

The channel list and both caps are configurable without code changes:
  YOUTUBE_ROSTER_MAX_CHANNELS       (default 300)
  YOUTUBE_ROSTER_VIDEOS_PER_CHANNEL  (default 10)

Pipeline per run:
  1. seed backfill: channel_ids already on youtube breaks -> roster
  2. playlistItems.list per active channel (hottest first) -> recent video ids
  3. videos.list (batched, 50 ids/call) with part=snippet,liveStreamingDetails
  4. same normalize + title/date filter as the search poller
     (normalize_youtube_video / looks_like_real_break / is_upcoming_or_live)
  5. upsert hits into breaks (no slice wipe — the search poller owns the
     youtube slice lifecycle) + stamp roster last_hit_at / last_checked_at
"""
import os
import re
import sys

import httpx

from . import config, db
from .normalizer import looks_like_real_break
from .youtube import (
    _parse_ts,
    channel_countries,
    estimate_quota_units,
    get_api_key,
    is_upcoming_or_live,
    normalize_youtube_video,
    videos_details,
)

PLAYLIST_ITEMS_URL = "https://www.googleapis.com/youtube/v3/playlistItems"

MAX_ROSTER_CHANNELS = int(os.environ.get("YOUTUBE_ROSTER_MAX_CHANNELS", "300"))
VIDEOS_PER_CHANNEL = int(os.environ.get("YOUTUBE_ROSTER_VIDEOS_PER_CHANNEL", "10"))

# Home countries for roster channels that don't set snippet.country on
# YouTube. Only confident, evidence-backed entries — everything else stays
# NULL (= unknown region) rather than guessed.
KNOWN_COUNTRIES = {
    # Gold Coast Trading Cards: NRL/AFL rugby league niche, .net site
    "UC9_1XxNlE817786QYm7AbXg": "AU",
    # Poom Breaks: Taiwan-based (poombreaks.live)
    "UCGzeXkDhxnYkRM-8rZZvoaQ": "TW",
    # Maritime Sports Cards: maritimesportscards.com (Canada)
    "UCsHDlC4em4oZcBl49zlxJwQ": "CA",
    # CNC Breaks (CloutsnChara): Kitchener ON store
    "UCM1CnVA0viwqwoK3lAJ7clA": "CA",
    # Out Of The Box: Ottawa ON brick & mortar
    "UC3XMSBs56tO133hlF_N8VQQ": "CA",
    # ByThaCard: Taiwan group-break operation (API country blank; ID-confirmed)
    "UC5rRnVt4XD_BBX3tzUt48Lg": "TW",
    # ByThaCard 2: same operation (API reports TW; ID-pinned)
    "UCI5LAOCy4SHLoRsNGjxr8Aw": "TW",
    # KwiatuCards: Polish breaker (API reports PL; ID-pinned)
    "UCf15ztZMysW3n8rXxOqeOXg": "PL",
    # Flying V Cards: Canadian hockey group breaks (Brian approved 2026-10-07)
    "UCzrJQ1QGG19T2Vmc-9CWH_w": "CA",
    # Ty The Card Guy: Edmonton AB hockey group breaks (Brian approved 2026-10-07)
    "UCxnQmu6CJTaEIqEYWvmeCvg": "CA",
    # Hokej Karty: Czech hockey card shop group breaks (Brian approved 2026-10-07)
    "UCTm16TWigrtCUgIWv-7Y-1Q": "CZ",
    # --- Researched 2026-10-11: channels not setting snippet.country ---
    # Ozzie Collectables: Melbourne AU B&M card shop (TCG sweep 2026-10-11)
    "UC9WQU0HbGGfxnOQ2Bft4Njg": "AU",
    # RNGmart: Aussie TCG rip & ship, rngmart.com.au (TCG sweep 2026-10-11)
    "UCzm8YqSFfek1PyMS5w9VhIQ": "AU",
    # Philmington: Florida corp records; US hobby figure
    "UClSazuEj3e0c_q8OX-JtNbQ": "US",
    # Phil's Pulls: ex-Yankees pitcher Phil Hughes
    "UC4zH7QBYi7MT4d5HuhFulCg": "US",
    # Southwest Missouri Box Breaks: Missouri in channel name
    "UC0-KR087k3WBVjTu8npmojg": "US",
    # Jason's Jet Breakers: founder is US Air Force pilot/tax lawyer
    "UCXFMq9LL2blU7w0ph-v5YNQ": "US",
    # Paradise Card Breaks: Town Square, Las Vegas NV
    "UCwgK1TlL3ZAHFhaJQKDur2A": "US",
    # CardCollector2_Breaks: physical store, Grove City OH
    "UCJxLZQKPXThfFKqs0A6ygMQ": "US",
    # HobbyLegends: eBay seller location "United States"
    "UC18DbVup8vtTn0mhe5sapAw": "US",
    # Triple Diamond Sports: brick & mortar, Eagan MN
    "UC2nBjhaUh9G7Uh96vKEPKMQ": "US",
    # Ultimate Sportcards: local shop, North Bay ON
    "UCHIg0idFC-k-ixqZOMfsxRw": "CA",
    # Batflip Breaks: via Midwest Box Breaks, ET schedule
    "UC4CdQqyJmEXO0kSTo2LxYFQ": "US",
    # BREAKfast Cards: (?) USD pricing; ships worldwide; no address found
    "UCztFnyVxFxrHth5FAcQ5RAw": "US",
    # Cards in the Land: 17800 Chillicothe Rd, Chagrin Falls OH
    "UCrm1MFFCqHkg3RROCCLRu0w": "US",
    # Tandem Collectibles: (?) .com Shopify; no location found
    "UCyPlr1b2qCfzr6jzHVIIQ1A": "US",
    # Prime Pulls TCG: Prime Pulls Card Shop, Yakima WA
    "UCUXZsz3TCngc4idailyfVhQ": "US",
    # Pack Hype Collective: eBay seller in Spring Hill FL
    "UC5izyuTTqgdwzu8ZQirnQPw": "US",
    # The GroupBreakers: (?) USD pricing + Venmo; no address found
    "UC_PjmujGaQkPa1GCEoSL8Iw": "US",
    # Bizzy Breaks: channel desc "Located in BC" with Canada flag
    "UC9JhDPM4rRj9yWfDMjPxB_g": "CA",
    # TheTCGbrothers: eBay seller, Mount Clemens MI
    "UC-NmZMN97iUdPrcaEQc3vYQ": "US",
    # Black Gold Sports Cards: 2327 Silvernail Rd, Pewaukee WI
    "UCZhJdE5P_IqKuqoAEOABxGA": "US",
    # RIPANDLIST: Dakota Gaming Supply Inc, US flag in bio
    "UC4f1Q1pmTgkUNiyCiqLIhcA": "US",
    # Cajun Breaker: Cajun branding = Louisiana
    "UCzHcm9GuQEunW6oCjYh_13Q": "US",
    # Breaks N' Takes 2: eBay seller, Gardena CA
    "UCP0D6Qrn3DTtmhrpxgDagmg": "US",
    # Stlcards6: St. Louis in channel name
    "UCzLJBNJyCCSpUakfcp6Hz9w": "US",
    # JunkWaxVintage: "based in Austin, TX"
    "UCUm8Lf9N5o3E7s05ojIVPyQ": "US",
    # 718 Breaks: 718 = NYC area code
    "UCLGwtWJbQfWReI5fn7bYQlw": "US",
    # BLUNT COLLECTIONS NZ: "New Zealand's home of live breaks"
    "UC1_hSoZlZAsQj4vtWee54tQ": "NZ",
    # ThePokePair!: ThePokePair LLC, US-based
    "UCvKOmHF0puJrv6Q_a3imhSQ": "US",
    # Long Ball Cards: USD pricing, baseball breaker
    "UC-huOEAW2s3UtCEKbsc23BQ": "US",
    # Big Breaks: 106 Oak Hill Ave, Wrentham MA
    "UC2-U3zqIDpoIFn5HmATOg8g": "US",
    # AllYouCanBreak: "Hong Kong Sports Card Group Breaks"
    "UCLsYvea08KZBO8IHv7Ob-cQ": "HK",
    # SportsBoxBreaks.com: Las Vegas NV & Pennsylvania
    "UCDuXqiKkABHh6bSbIUOOxhg": "US",
    # Rickman Rips: German live group breaks, rickmanbreaks.de (Brian approved 2026-10-11, weekly discovery)
    "UCXTn7VWwEfWdu2PsXDLbS9A": "DE",
    # FootballCardsZone: Polish numbered soccer breaks, footballcardszone.com (Brian approved 2026-10-11, weekly discovery)
    "UCRKndqE4EVtwSusFq60vgRA": "PL",
    # GP Sports Cards: Canadian hockey group breaks, gpsportscards.com (Brian approved 2026-10-13, weekly discovery)
    "UC_r23ILOYKMgrwF5--zO3oQ": "CA",
    # JC CardsMania (Jonas Carlsson): Swedish NHL group breaks incl. SHL product; region inferred from Swedish metadata (Brian approved 2026-10-13, weekly discovery PM)
    "UCpS0vGkENx48k0s146NUm7w": "SE",
}

# Title-substring fallback for discovered channels whose IDs aren't in
# KNOWN_COUNTRIES (e.g. search-poller discoveries). Matched case-insensitively
# against the roster channel title; same confidence bar as above.
KNOWN_COUNTRIES_BY_TITLE = {
    # ByThaCard: Taiwan group-break operation (traditional Chinese titles)
    "bythacard": "TW",
    # KwiatuCards: Polish breaker (Polish-language break titles)
    "kwiatucards": "PL",
}


def _override_country(cid: str, *titles: str | None) -> str | None:
    """Curated country for a channel: by ID, then by title substring.

    Accepts multiple candidate titles (roster title, video-details title)
    — any match wins. Only confident, evidence-backed entries live in the
    maps below; everything else stays NULL (= unknown region).
    """
    if cid in KNOWN_COUNTRIES:
        return KNOWN_COUNTRIES[cid]
    for title in titles:
        t = re.sub(r"[^a-z0-9]", "", (title or "").lower())
        for sub, country in KNOWN_COUNTRIES_BY_TITLE.items():
            if sub and sub in t:
                return country
    return None


def uploads_playlist_id(channel_id: str) -> str:
    """Deterministic uploads-playlist id for a channel.

    YouTube channel ids start with "UC"; the channel's uploads playlist is
    the same id with a "UU" prefix. Deriving it saves a channels.list call
    (1 unit) per channel per run.
    """
    if not channel_id or not channel_id.startswith("UC") or len(channel_id) <= 2:
        raise ValueError(f"unexpected channel id format: {channel_id!r}")
    return "UU" + channel_id[2:]


def _get(url: str, params: dict) -> dict:
    """GET with the API key attached. Never logs the URL (it holds the key)."""
    params = dict(params)
    params["key"] = get_api_key()
    resp = httpx.get(url, params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


def playlist_recent_video_ids(channel_id: str, max_results: int = 10) -> list[str]:
    """One playlistItems.list call (1 quota unit): recent uploads' video ids."""
    data = _get(PLAYLIST_ITEMS_URL, {
        "part": "contentDetails",
        "playlistId": uploads_playlist_id(channel_id),
        "maxResults": min(max_results, 50),
    })
    vids = []
    for item in data.get("items", []) or []:
        vid = ((item.get("contentDetails") or {}).get("videoId")) or ""
        if vid:
            vids.append(vid)
    return vids


def estimate_roster_quota_units(n_channels: int, n_videos: int) -> int:
    """Quota cost of a roster run: 1 unit per playlistItems call + 1 per
    videos.list batch. n_channels playlist calls, ceil(n_videos/50) batches."""
    n_detail_calls = (n_videos + 49) // 50 if n_videos else 0
    return estimate_quota_units(0, n_channels + n_detail_calls)


def fetch_roster_breaks(conn):
    """Poll active roster channels and return kept break rows.

    Returns (rows, hit_channel_ids, checked_channel_ids, channel_of_video,
    not_live_vids, stats). channel_of_video maps every seen video id to its
    channel (all from successfully checked channels) — the caller uses it
    to demote live rows the roster no longer sees. not_live_vids holds seen
    video ids whose details were fetched and show the stream is over (not
    broadcasting, not upcoming) — the caller demotes those too, since the
    not-in-window rule alone misses ended streams still in the window.
    Writes nothing — the caller upserts rows and stamps the roster, so a
    total API failure leaves existing data untouched (no slice wipe here
    at all).
    """
    get_api_key()  # fail fast with a clear message when the key is missing

    channels = db.get_roster_channels(conn, MAX_ROSTER_CHANNELS)
    if not channels:
        print("youtube-roster: roster is empty — discovery hook will fill it "
              "as the search poller keeps breaks.")
        return [], [], [], {}, set(), {
            "n_channels": 0, "n_api_ok": 0, "units": 0,
            "n_kept": 0, "n_non_break": 0, "n_past": 0, "n_no_date": 0,
        }

    # 1. recent video ids per channel (1 quota unit each), deduped
    video_ids: list[str] = []
    channel_of_video: dict[str, str] = {}
    checked: list[str] = []
    n_playlist_calls = 0
    for ch in channels:
        cid = ch["channel_id"]
        try:
            vids = playlist_recent_video_ids(cid, VIDEOS_PER_CHANNEL)
        except httpx.HTTPStatusError as exc:
            print(f"  roster playlist failed ({cid}): "
                  f"HTTP {exc.response.status_code} — skipping",
                  file=sys.stderr)
            continue
        n_playlist_calls += 1
        checked.append(cid)
        for vid in vids:
            if vid and vid not in channel_of_video:
                channel_of_video[vid] = cid
                video_ids.append(vid)

    # 2. details in 50-id batches (1 quota unit per batch)
    details = videos_details(video_ids) if video_ids else {}

    # 3. home countries for channels still unknown: YouTube's own
    #    snippet.country first (1 unit per 50 channels), then the curated
    #    overrides. Titles come from the video details — roster titles are
    #    NULL for discovered channels, so the roster table alone can't
    #    drive the title fallback.
    country_of = {ch["channel_id"]: ch.get("country") for ch in channels}
    roster_title_of = {ch["channel_id"]: ch.get("title") for ch in channels}
    detail_title_of: dict[str, str] = {}
    for vid, det in details.items():
        snip = (det or {}).get("snippet", {})
        cid = snip.get("channelId") or channel_of_video.get(vid)
        if cid and snip.get("channelTitle"):
            detail_title_of.setdefault(cid, snip["channelTitle"])
    missing = [cid for cid, c in country_of.items() if not c]
    n_country_calls = 0
    if missing:
        n_country_calls = (len(missing) + 49) // 50
        try:
            api_map = channel_countries(missing)
        except Exception as exc:
            print(f"  roster country lookup failed: {exc}", file=sys.stderr)
            api_map = {}
        fresh = {cid: c for cid, c in api_map.items() if c}
        for cid in missing:
            if cid not in fresh:
                override = _override_country(
                    cid, roster_title_of.get(cid), detail_title_of.get(cid))
                if override:
                    fresh[cid] = override
        n_filled = db.set_channel_countries(conn, fresh)
        country_of.update(fresh)
        if n_filled:
            print(f"youtube-roster: backfilled country for {n_filled} channels")

    # 4. normalize + keep only real, joinable breaks (same filter as search)
    rows: list[dict] = []
    hit_channels: set[str] = set()
    not_live_vids: set[str] = set()
    n_non_break = n_past = n_no_date = 0
    for vid in video_ids:
        row = normalize_youtube_video(vid, None, details.get(vid))
        if not (row and row.get("source_url")):
            continue
        # Confirmed-over evidence for the caller's demote pass: a row here
        # means the details fetch succeeded, and YouTube says this video is
        # neither broadcasting now nor scheduled ahead — an ended stream
        # still sitting in the playlist window (front-end audit 2026-10-12:
        # ~50 such streams stayed tagged LIVE). Videos whose details fetch
        # failed never produce a row, so they can't be demoted on a guess.
        if not row.get("is_live") and not is_upcoming_or_live(row):
            not_live_vids.add(vid)
        if not looks_like_real_break(row["title_raw"], row.get("format"),
                                       row.get("breaker")):
            n_non_break += 1
        elif is_upcoming_or_live(row):
            row["country"] = country_of.get(channel_of_video[vid])
            rows.append(row)
            hit_channels.add(channel_of_video[vid])
        elif _parse_ts(row.get("starts_at")) is None:
            n_no_date += 1
        else:
            n_past += 1

    units = estimate_roster_quota_units(n_playlist_calls, len(video_ids)) + n_country_calls
    print(f"youtube-roster: {n_playlist_calls} playlist + "
          f"{(len(video_ids) + 49) // 50 if video_ids else 0} detail + "
          f"{n_country_calls} country calls "
          f"= ~{units} quota units across {len(checked)} channels; "
          f"{len(rows)} upcoming breaks kept, "
          f"{n_non_break} non-break / {n_past} past / {n_no_date} no-date dropped")
    return rows, sorted(hit_channels), checked, channel_of_video, not_live_vids, {
        "n_channels": len(channels),
        "n_api_ok": n_playlist_calls,
        "units": units,
        "n_kept": len(rows),
        "n_non_break": n_non_break,
        "n_past": n_past,
        "n_no_date": n_no_date,
    }
