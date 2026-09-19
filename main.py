import argparse
import logging
import sys
import tempfile
from datetime import date
from pathlib import Path

import collect as collector
import config as config_loader
import money
import slack
import store
import workbook
import xyne

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("cost-anomaly")


def run(dry_run: bool = False, target: date | None = None, provider_name: str | None = None,
        out_path: str | None = None, no_post: bool = False) -> int:
    cfg = config_loader.load(provider=provider_name)
    target = target or collector.default_target()

    report = collector.collect(cfg, target)

    t = report["totals"]
    log.info("%s: total %s across %d sections",
             target, money.fmt(cfg, t["grand_total"]), len(report["sections"]))

    if out_path:
        path = out_path
    else:
        tmp = tempfile.mkdtemp(prefix="cost-anomaly-")
        path = str(Path(tmp) / f"{target.isoformat()}-cloud-costs.xlsx")

    workbook.build(path, cfg, report)
    log.info("Workbook written to %s", path)

    # Persist to the Control-Center store. Fully isolated: write_report already
    # swallows its own errors, and this outer guard means even an unexpected one
    # (or a vendor session-token expiry) can never lose a run that otherwise
    # built the report and is about to deliver it.
    try:
        store.write_report(cfg, report)
    except Exception as e:
        log.error("Cost store write failed (continuing): %s", e)

    if dry_run or no_post:
        _print_summary(cfg, report, path)
        return 0

    # Each destination is independent: a failure posting to one must not stop the
    # other, or a Xyne outage would silently cost us the Slack report too.
    failures = []
    for name, fn in (("Slack", slack.post), ("Xyne", xyne.post)):
        try:
            fn(cfg, report, path)
        except Exception as e:
            log.error("%s delivery failed: %s", name, e)
            failures.append(name)
    if failures:
        log.error("Delivery failed for: %s", ", ".join(failures))
        return 1
    return 0


def _print_summary(cfg: dict, report: dict, path: str) -> None:
    t = report["totals"]
    print(f"\nReport for {report['date']}  (workbook: {path})")
    print("-" * 72)
    dec = 0 if cfg["report_currency"] == "INR" else 2
    show_inv = any(collector.credits_visible(s["total_report"], s["total_invoiced_report"], dec)
                   for s in report["sections"])
    for b in t["buckets"]:
        share = (b["total"] / t["grand_total"] * 100.0) if t["grand_total"] else 0.0
        line = f"  {b['label']:<28} {money.fmt(cfg, b['total']):>16}  ({share:5.2f}%)"
        if show_inv:
            line += f"   invoiced {money.fmt(cfg, b['total_invoiced'])}"
        print(line)
    total_line = f"  {'Total Cloud Costs':<28} {money.fmt(cfg, t['grand_total']):>16}"
    if show_inv:
        total_line += f"   invoiced {money.fmt(cfg, t['grand_total_invoiced'])}"
    print(total_line)
    rides = report.get("rides")
    if rides and rides.get("total"):
        print(f"  {'Total Rides':<28} {rides['total']:>16,}")
        for src, n in (rides.get("by_source") or {}).items():
            print(f"    {src:<26} {n:>16,}")
        # Exactly the bases the Slack message and workbook publish, so a dry run
        # can never disagree with what would actually be posted.
        for e in collector.unit_economics(cfg, report):
            print(f"  {e['label']:<28} {money.fmt(cfg, e['per_ride'], decimals=2):>16}"
                  f"   ({money.fmt(cfg, e['cost'])} / {e['rides']:,})")
    else:
        print(f"  {'Total Rides':<28} {'unavailable':>16}")
    print("-" * 72)
    for s in sorted(report["sections"], key=lambda x: x["total_report"], reverse=True):
        print(f"  {s['label']:<28} {money.fmt(cfg, s['total_report']):>16}  "
              f"{len(s['rows'])} services")
    print()


def main():
    ap = argparse.ArgumentParser(description="Daily multi-cloud cost report to Slack.")
    ap.add_argument("--provider", choices=("aws", "gcp", "all"),
                    help="Which clouds to report on. Overrides config/env.")
    ap.add_argument("--dry-run", action="store_true",
                    help="Build the workbook and print the summary, but don't post to Slack")
    ap.add_argument("--out", help="Write the workbook to this path instead of a temp dir")
    ap.add_argument("--date", help="Target date (YYYY-MM-DD). Default: T-2 (day before yesterday)")
    ap.add_argument("--backfill", type=int, metavar="N",
                    help="Populate the cost store for the last N days ending at the target "
                         "day (inclusive), then exit. No Slack post.")
    args = ap.parse_args()

    if args.backfill:
        cfg = config_loader.load(provider=args.provider)
        end = (date.fromisoformat(args.date) if args.date else collector.default_target()) \
            + __import__("datetime").timedelta(days=1)          # exclusive
        start = end - __import__("datetime").timedelta(days=args.backfill)
        counts = store.backfill(cfg, start, end)
        print(f"Backfilled {start}..{end - __import__('datetime').timedelta(days=1)} "
              f"(inclusive): {counts}")
        sys.exit(0)

    target = date.fromisoformat(args.date) if args.date else None
    sys.exit(run(dry_run=args.dry_run, target=target, provider_name=args.provider,
                 out_path=args.out))


if __name__ == "__main__":
    main()
