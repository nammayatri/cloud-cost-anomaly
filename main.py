import argparse
import json
import logging
import sys
from datetime import date, timedelta

import config as config_loader
import providers
import slack
from detect import detect
from drilldown import top_movers

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("cost-anomaly")

_TITLES = {"aws": "AWS", "gcp": "GCP"}


def run(dry_run: bool = False, csv_path: str | None = None, target: date | None = None,
        provider_name: str | None = None) -> int:
    cfg = config_loader.load(provider=provider_name)
    provider = providers.get(cfg["provider"])
    title = _TITLES[cfg["provider"]]
    target = target or (date.today() - timedelta(days=1))

    slack.set_currency(cfg["currency"])
    slack.set_unit_style(cfg["provider"])

    if csv_path:
        log.info("Loading cost data from CSV %s", csv_path)
        scoped = provider.load_csv(csv_path)
    else:
        log.info("Fetching %s cost data", title)
        scoped = provider.fetch_by_service(cfg, end=target + timedelta(days=1))

    if not scoped:
        log.error("No cost data available")
        return 1

    results = []
    for scope, df in scoped.items():
        if target not in df.index:
            log.warning("%s: target date %s not in data (have %s..%s) — skipping scope",
                        scope, target, df.index.min(), df.index.max())
            continue

        increases, decreases, summary = detect(df, target, cfg)
        log.info("%s: %d increases, %d decreases for %s (total %.2f %s)",
                 scope, len(increases), len(decreases), target, summary["total"], cfg["currency"])

        movers: dict[str, list[dict]] = {}
        if not csv_path:
            for a in increases + decreases:
                try:
                    movers[a.service] = top_movers(cfg, provider, scope, a.service, target)
                except Exception as e:
                    log.warning("Drilldown failed for %s/%s: %s", scope, a.service, e)
                    movers[a.service] = []

        results.append({
            "scope": scope, "summary": summary,
            "increases": increases, "decreases": decreases, "movers": movers,
        })

    if not results:
        log.error("Target date %s not present in any scope", target)
        return 1

    # Zero-noise contract: stay silent unless something crossed a threshold somewhere.
    if not any(r["increases"] or r["decreases"] for r in results):
        log.info("No threshold crossings — skipping Slack post.")
        return 0

    if dry_run:
        combined = slack.combine_summaries(results)
        payload = {
            "header": slack.build_header_payload(
                combined,
                sum(len(r["increases"]) for r in results),
                sum(len(r["decreases"]) for r in results),
                cfg.get("mention", ""),
                title=title,
                scope_rows=[(r["scope"], r["summary"], len(r["increases"]), len(r["decreases"])) for r in results],
            ),
            "thread_attachments": slack.build_thread_attachments_multi(results),
        }
        print(json.dumps(payload, indent=2, default=str))
        return 0

    slack.post(cfg, results, title=title)
    log.info("Posted to Slack channel %s", cfg["slack_channel_id"])
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", choices=("aws", "gcp"), help="Cloud to report on. Overrides config/env.")
    ap.add_argument("--dry-run", action="store_true", help="Print Slack payload to stdout instead of posting")
    ap.add_argument("--csv", help="Use a local CSV instead of the cost API (AWS only; skips drill-down)")
    ap.add_argument("--date", help="Target date (YYYY-MM-DD). Default: yesterday")
    args = ap.parse_args()

    target = date.fromisoformat(args.date) if args.date else None
    sys.exit(run(dry_run=args.dry_run, csv_path=args.csv, target=target, provider_name=args.provider))


if __name__ == "__main__":
    main()
