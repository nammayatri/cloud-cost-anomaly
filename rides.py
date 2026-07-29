"""Ride counts from ClickHouse, for the cost-per-ride headline.

The SQL lives OUTSIDE this repository, in a mounted directory (a ConfigMap in
Kubernetes). Two reasons:

  * This repo is public. The queries embed internal database, table and column
    names — schema is not something to publish alongside the tool.
  * What counts as "a ride" is a business definition, not a property of the cost
    report. It changes when the product changes, and it should be editable by the
    people who own that definition without a code change or a redeploy.

Query contract
--------------
Every `*.sql` file in `ride_query_dir` is executed once per run, in filename
order. The filename becomes that source's label, with any numeric ordering prefix
stripped ("10-rides.sql" -> "rides"). Sources are reported separately as well as
summed, so the total is always decomposable.

Each query must:
  * contain `{day}` where the target date belongs (substituted as YYYY-MM-DD),
  * return either  `cloud <TAB> count`  (preferred — enables per-cloud unit
    economics) or a single `count` column,
  * end with `FORMAT TabSeparated`.

Talks to ClickHouse over the HTTP interface (8123) so the only dependency is
`requests` — a handful of aggregate queries a day does not justify a driver.
"""

import logging
import re
from pathlib import Path

import requests

log = logging.getLogger("cost-anomaly.rides")

_TIMEOUT = 60

# Rides whose cloud_type is NULL or UNAVAILABLE. They are real rides and must
# count toward the total, but they cannot be charged to a cloud.
UNATTRIBUTED = "UNATTRIBUTED"


def configured(cfg: dict) -> bool:
    return bool(cfg.get("clickhouse_host") and cfg.get("clickhouse_user")
                and cfg.get("ride_query_dir"))


def _url(cfg: dict) -> str:
    scheme = "https" if cfg.get("clickhouse_secure") else "http"
    return f"{scheme}://{cfg['clickhouse_host']}:{cfg['clickhouse_port']}/"


def _query(cfg: dict, sql: str) -> str:
    resp = requests.post(
        _url(cfg),
        params={"database": cfg["clickhouse_database"]},
        data=sql.encode("utf-8"),
        auth=(cfg["clickhouse_user"], cfg.get("clickhouse_password") or ""),
        timeout=_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.text.strip()


def _label_for(stem: str) -> str:
    """Display label from a filename stem.

    Files are named with a numeric prefix ("10-rides.sql") so they sort
    deterministically, but that prefix is an ordering device for the filesystem —
    it must not surface in the report. "10-rides" reads as a quantity or a code to
    anyone who hasn't seen the directory.
    """
    stem = re.sub(r"^\d+[-_]", "", stem)
    return stem.replace("-", " ").replace("_", " ").strip() or "rides"


def _load_queries(cfg: dict) -> list[tuple[str, str]]:
    """(label, sql) for every .sql in the query dir, in filename order."""
    d = Path(cfg["ride_query_dir"])
    if not d.is_dir():
        log.warning("ride_query_dir %s does not exist — skipping ride metrics", d)
        return []
    out = []
    for path in sorted(d.glob("*.sql")):
        text = path.read_text().strip()
        if not text:
            continue
        if "{day}" not in text:
            # Silently running a query with no date bound would count the whole
            # table and produce a wildly wrong cost-per-ride, so refuse it.
            log.error("%s has no {day} placeholder — refusing to run it unbounded", path.name)
            continue
        out.append((_label_for(path.stem), text))
    if not out:
        log.warning("No usable .sql files in %s", d)
    return out


def _parse(raw: str) -> dict[str, int]:
    """Parse `cloud<TAB>count` rows, or a bare count, into {cloud: n}."""
    by_cloud: dict[str, int] = {}
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) == 1:
            by_cloud[UNATTRIBUTED] = by_cloud.get(UNATTRIBUTED, 0) + int(parts[0])
            continue
        cloud = (parts[0].strip() or UNATTRIBUTED)
        if cloud in ("UNAVAILABLE", "\\N", "NULL"):
            cloud = UNATTRIBUTED
        by_cloud[cloud] = by_cloud.get(cloud, 0) + int(parts[1])
    return by_cloud


def counts_for_day(cfg: dict, day) -> dict | None:
    """Ride counts for `day`, summed across every configured query.

    Returns {"total": int, "by_cloud": {...}, "by_source": {label: int}} or None
    when ClickHouse isn't configured or reachable. A missing ride count degrades
    the report to cost-only rather than failing the run — the cost data is worth
    sending on its own.
    """
    if not configured(cfg):
        log.info("ClickHouse or ride_query_dir not configured — skipping ride metrics")
        return None

    queries = _load_queries(cfg)
    if not queries:
        return None

    by_cloud: dict[str, int] = {}
    by_source: dict[str, int] = {}
    failed: list[str] = []

    for label, sql in queries:
        try:
            raw = _query(cfg, sql.replace("{day}", day.isoformat()))
        except Exception as e:
            # One failing source must not silently shrink the denominator — a
            # partial ride count would inflate cost-per-ride and look like a real
            # regression. Record it and refuse to report a total below.
            log.error("Ride query %r failed: %s", label, e)
            failed.append(label)
            continue
        counts = _parse(raw)
        by_source[label] = sum(counts.values())
        for cloud, n in counts.items():
            by_cloud[cloud] = by_cloud.get(cloud, 0) + n
        log.info("Ride source %r for %s: %d", label, day, by_source[label])

    if failed:
        log.error("Ride sources %s failed — omitting ride metrics rather than "
                  "reporting an understated total", failed)
        return None

    total = sum(by_source.values())
    if not total:
        log.warning("No rides found for %s", day)
    return {"total": total, "by_cloud": by_cloud, "by_source": by_source}
