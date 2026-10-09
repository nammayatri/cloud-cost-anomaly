import base64
import concurrent.futures
import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta

log = logging.getLogger("cost-anomaly.exotel")


def configured(cfg: dict) -> bool:
    return bool(cfg.get("exotel_api_url") and cfg.get("exotel_account_sid")
                and cfg.get("exotel_api_key") and cfg.get("exotel_api_token"))


def _auth_header(cfg: dict) -> str:
    token = f"{cfg['exotel_api_key']}:{cfg['exotel_api_token']}".strip()
    return "Basic " + base64.b64encode(token.encode()).decode()


def _get(cfg: dict, url: str) -> dict:
    """GET with a retry on 429 — Exotel's rate limit is real (confirmed against
    a live account) and gives no Retry-After header, so backoff is a fixed,
    doubling schedule (exotel_retry_attempts / exotel_retry_base_delay) rather
    than anything server-advised. Any other failure (4xx/5xx besides 429,
    timeout) raises immediately — that's a real problem for the caller to see,
    not the vendor asking us to slow down.
    """
    attempts = int(cfg.get("exotel_retry_attempts", 6))
    base_delay = int(cfg.get("exotel_retry_base_delay", 5))
    timeout = int(cfg.get("exotel_timeout", 45))
    req = urllib.request.Request(url, headers={"Authorization": _auth_header(cfg)})
    last_err = None
    for attempt in range(attempts):
        try:
            resp = urllib.request.urlopen(req, timeout=timeout)
            return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            if e.code != 429:
                raise
            last_err = e
            if attempt + 1 < attempts:
                delay = base_delay * (2 ** attempt)
                log.warning("Exotel rate-limited (attempt %d/%d) — waiting %ds",
                            attempt + 1, attempts, delay)
                time.sleep(delay)
    raise last_err


def _day_windows(day: date, window_minutes: int) -> list[tuple[str, str]]:
    """Split one calendar day into consecutive DateCreated windows of
    `window_minutes` each, covering 00:00:00 through 23:59:59 with no gaps
    or overlaps (the last window is shorter if window_minutes doesn't divide
    evenly into a day)."""
    start = datetime.combine(day, datetime.min.time())
    end = start + timedelta(days=1)
    delta = timedelta(minutes=window_minutes)
    windows = []
    cur = start
    while cur < end:
        window_end = min(cur + delta, end) - timedelta(seconds=1)
        windows.append((cur.strftime("%Y-%m-%d %H:%M:%S"), window_end.strftime("%Y-%m-%d %H:%M:%S")))
        cur += delta
    return windows


def _fetch_window(cfg: dict, sid: str, gte: str, lte: str) -> dict[str, dict]:
    """Paginate through every call in one narrow DateCreated window, summing
    Price and counting calls per Direction.

    Real accounts run well over 100k calls/day, and the API hard-caps
    PageSize at 100 (confirmed against a live account, not assumed from
    docs) — a single whole-day fetch means 1000+ sequential pages, around
    15 minutes. Windows are kept narrow (see fetch_day) specifically so each
    one finishes in a handful of pages.
    """
    api_root = cfg["exotel_api_url"].rstrip("/")
    page_size = int(cfg.get("exotel_page_size", 100))
    max_pages = int(cfg.get("exotel_max_pages_per_window", 3000))

    date_filter = f"gte:{gte};lte:{lte}"
    url = (f"{api_root}/v1/Accounts/{sid}/Calls.json?"
           + urllib.parse.urlencode({"DateCreated": date_filter, "PageSize": page_size}))

    totals: dict[str, dict] = {}
    for _ in range(max_pages):
        data = _get(cfg, url)
        for call in data.get("Calls", []) or []:
            direction = call.get("Direction") or "unknown"
            slot = totals.setdefault(direction, {"cost": 0.0, "units": 0})
            slot["cost"] += float(call.get("Price") or 0.0)
            slot["units"] += 1

        next_uri = (data.get("Metadata") or {}).get("NextPageUri")
        if not next_uri:
            break
        url = api_root + next_uri
    else:
        log.warning("Exotel window %s..%s hit the %d-page safety cap — volume may be "
                    "undercounted", gte, lte, max_pages)
    return totals


def fetch_day(cfg: dict, day: date) -> list[dict]:
    """Per-direction Exotel cost for one day, straight from the vendor's own
    `Price` field.

    Returns [{"account", "service": direction, "cost": float, "units": count}].
    Raises on HTTP/auth failure so the caller can tell "unavailable" apart from
    "zero calls that day" (an empty list) — same contract as the other vendor.
    Any one window's failure fails the whole day.

    Fetched as windows of exotel_window_minutes each (default 60 — 24 windows
    a day), run exotel_max_workers at a time. Exotel's pagination is
    cursor-based — page 2's URL only exists once page 1's response arrives, so
    pages within one window can't be parallelized. But windows are independent
    (bounded by date filter, not by a cursor), so fetching several at once cuts
    a day's wall-clock time from ~15 minutes (one sequential pass) down
    sharply. exotel_max_workers defaults conservatively (2) because a live
    account hit a 429 at higher concurrency during testing, with no
    Retry-After to size a safe value from — raise it cautiously.
    """
    sid = cfg["exotel_account_sid"]
    window_minutes = int(cfg.get("exotel_window_minutes", 60))
    max_workers = int(cfg.get("exotel_max_workers", 2))
    windows = _day_windows(day, window_minutes)

    merged: dict[str, dict] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [pool.submit(_fetch_window, cfg, sid, gte, lte) for gte, lte in windows]
        for f in futures:
            for direction, v in f.result().items():
                slot = merged.setdefault(direction, {"cost": 0.0, "units": 0})
                slot["cost"] += v["cost"]
                slot["units"] += v["units"]

    account = cfg.get("exotel_account_label") or "Exotel"
    return [{"account": account, "service": direction, "cost": v["cost"], "units": float(v["units"])}
            for direction, v in merged.items()]
