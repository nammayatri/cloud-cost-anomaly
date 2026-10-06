"""Currency normalisation for the combined report.

Every cloud reports in its own currency — Cost Explorer is USD-only, while a
billing export uses whatever the billing account is denominated in. The workbook
has to add them together, so one `report_currency` is chosen and everything is
converted into it before any arithmetic happens.

The rate is fetched for the DAY BEING REPORTED, not "latest", so re-running an
old date reproduces the same figures. A configured rate is kept as a fallback for
when the lookup fails, and `fx_source` records which of the two was used — a
report that silently switched rates would be unauditable after the fact.
"""

import logging
import time

import requests

log = logging.getLogger("cost-anomaly.money")

_SYMBOLS = {"USD": "$", "INR": "₹", "EUR": "€", "GBP": "£"}

_FX_TIMEOUT = 20
_FX_ATTEMPTS = 3
_FX_BACKOFF = 2   # seconds, multiplied by attempt number


def resolve_rate(cfg: dict, target) -> None:
    """Fetch USD->INR for the target DAY and write it into cfg in place.

    The rate is fetched for the day being reported on, not "latest". Using today's
    rate to convert a two-day-old bill makes the report unreproducible — re-running
    it next week would silently produce different rupee totals for the same spend.
    Pinning it to the usage date means the number is stable forever.

    On any failure the configured `usd_inr_rate` stands. A cron that dies because a
    free FX endpoint had a bad minute is worse than one that reports at yesterday's
    rate and says so — `fx_source` records which happened, and the report prints it.
    """
    if not cfg.get("fx_fetch", True):
        cfg["fx_source"] = f"pinned ({cfg['usd_inr_rate']:g})"
        return

    # FX source chain, tried in order — each is a fallback for the one before, so
    # a single provider being down never forces the stale pinned rate:
    #   1. Frankfurter, for the TARGET day (date-specific → most correct, keyless).
    #   2. currencyapi.net live (keyed, reliable) — recent rate, close enough for a
    #      T-2 report when frankfurter is unavailable.
    #   3. The pinned usd_inr_rate.
    url = cfg.get("fx_api_url") or "https://api.frankfurter.app/{date}?from=USD&to=INR"
    last_err = None
    for attempt in range(_FX_ATTEMPTS):
        try:
            resp = requests.get(url.format(date=target.isoformat()), timeout=_FX_TIMEOUT)
            resp.raise_for_status()
            data = resp.json()
            fetched = float(data["rates"]["INR"])
            # A transposed or malformed response would quietly rescale the entire
            # report, so refuse anything outside a plausible band.
            if not 50.0 <= fetched <= 200.0:
                raise ValueError(f"implausible USD/INR rate {fetched}")
            cfg["usd_inr_rate"] = fetched
            cfg["fx_source"] = f"frankfurter @ {data.get('date', target.isoformat())}"
            log.info("USD/INR for %s = %.4f (%s)", target, fetched, cfg["fx_source"])
            return
        except Exception as e:
            last_err = e
            if attempt + 1 < _FX_ATTEMPTS:
                log.warning("frankfurter attempt %d/%d failed (%s); retrying",
                            attempt + 1, _FX_ATTEMPTS, e)
                time.sleep(_FX_BACKOFF * (attempt + 1))

    key = cfg.get("fx_currencyapi_key")
    if key:
        try:
            resp = requests.get(cfg.get("fx_currencyapi_url") or "https://currencyapi.net/api/v2/rates",
                                params={"key": key, "base": "USD", "output": "json"},
                                headers={"Accept": "application/json"}, timeout=_FX_TIMEOUT)
            resp.raise_for_status()
            fetched = float(resp.json()["rates"]["INR"])
            if not 50.0 <= fetched <= 200.0:
                raise ValueError(f"implausible USD/INR rate {fetched}")
            cfg["usd_inr_rate"] = fetched
            cfg["fx_source"] = "currencyapi.net (live)"
            log.info("USD/INR = %.4f (currencyapi.net live; frankfurter unavailable)", fetched)
            return
        except Exception as e:
            log.warning("currencyapi.net FX also failed (%s)", e)

    cfg["fx_source"] = f"pinned fallback ({cfg['usd_inr_rate']:g}) — fetch failed"
    log.warning("FX fetch failed after %d attempts (%s); using pinned rate %g — "
                "figures will not match a run that fetched successfully",
                _FX_ATTEMPTS, last_err, cfg["usd_inr_rate"])


def symbol(code: str) -> str:
    return _SYMBOLS.get(code, code + " ")


def rate(cfg: dict, frm: str, to: str) -> float:
    """Multiplier converting `frm` into `to`."""
    if frm == to:
        return 1.0
    if frm == "USD" and to == "INR":
        return float(cfg["usd_inr_rate"])
    if frm == "INR" and to == "USD":
        return 1.0 / float(cfg["usd_inr_rate"])
    raise RuntimeError(
        f"No configured FX rate for {frm}->{to}. Add one to money.rate() rather "
        f"than letting the report silently add mismatched currencies."
    )


def convert(cfg: dict, amount: float, frm: str, to: str | None = None) -> float:
    to = to or cfg["report_currency"]
    return amount * rate(cfg, frm, to)


def fmt(cfg: dict, amount: float, code: str | None = None,
        decimals: int | None = None) -> str:
    """Money for human display. INR is shown whole by default — rupee paise are
    noise on a ₹80,000 line; USD keeps cents.

    Pass `decimals` explicitly for unit economics. Cost per ride is a fraction of
    a rupee, so the default whole-rupee rounding would render it as "₹0" and throw
    away the entire number.
    """
    code = code or cfg["report_currency"]
    if decimals is None:
        decimals = 0 if code == "INR" else 2
    # A sum of credits against their own costs lands on -0.0 (or a femto-rupee
    # residue), which would print as "-₹0". Anything that rounds to zero is zero.
    if round(amount, decimals) == 0:
        amount = 0.0
    sign = "-" if amount < 0 else ""
    return f"{sign}{symbol(code)}{abs(amount):,.{decimals}f}"
