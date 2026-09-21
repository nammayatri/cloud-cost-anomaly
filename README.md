<div align="center">

# Cloud Cost Report

**Daily multi-cloud cost report — Slack summary + Excel workbook. AWS, GCP and Google Maps.**

A small Kubernetes CronJob that pulls every AWS account, every GCP project and the Google Maps Platform bill into one report: a Slack summary with cost-per-ride against goal, plus a workbook with a tab per account showing every service across the last 7 days.

<br/>

![License](https://img.shields.io/badge/license-MIT-blue.svg)
![Python](https://img.shields.io/badge/python-3.12-blue.svg)
![Platform](https://img.shields.io/badge/runs%20on-Kubernetes-326ce5.svg)
![Clouds](https://img.shields.io/badge/clouds-AWS%20%7C%20GCP-232f3e.svg)
![Slack](https://img.shields.io/badge/output-Slack-4A154B.svg)

<br/>

<img src="docs/flow.svg" alt="Pipeline animation" width="100%"/>

</div>

---

## ✨ Why this exists

Cloud billing surprises usually arrive on the **first of next month** — by then the leak has been running for weeks. This job flips that:

- **The whole bill in one place** — every AWS account, every GCP project, and Maps, added up in a single currency
- **Unit economics, not just totals** — cost per ride against goal, so spend is judged against the thing it buys
- **A week of context per service** — 7 daily columns, not a today/yesterday pair, so weekday rhythm is distinguishable from a real step change
- **Numbers that don't move** — targets T-2, after both clouds have finished restating

## 🧠 What the report contains

### Cost bases

Every total is reported on two bases whenever credits are non-zero:

* **Usage basis** — what the usage costs after commitment and negotiated discounts,
  before promotional credits. GCP excludes `credits.type = 'PROMOTION'` and the
  `Invoice / Contract billing adjustment` rows; AWS uses `UnblendedCost`. Every
  percentage, the movers ranking and the budget comparison use this basis, so a
  promotion starting or expiring never looks like a usage change.
* **Invoiced** — the billed figure after every credit. GCP `cost + all credits`,
  AWS `NetUnblendedCost`.

With no credits in play the two are identical and only one column is shown.

The run targets **T-2 (the day before yesterday)**, not T-1. Both AWS and GCP restate billing rows for roughly 24–48h after the usage day, and GCP is the slower of the two. Reporting T-1 means routinely publishing partial numbers that produce phantom drops which vanish overnight — the fastest way to teach a channel to ignore a bot. One extra day of latency buys numbers that never move after the fact.

**Slack message** — the headline: spend per cloud with contribution %, total, ride count, and cost-per-ride against goal. Then a per-account roll-up, and a threaded reply listing the biggest day-over-day increases (ranked by *money moved*, not percent — a 400% jump on a ₹20 service is trivia).

**Workbook** (threaded attachment) — one tab per account/project:

| Tab | Contents |
|---|---|
| `Summary` | Spend by cloud + share, total, rides, cost-per-ride vs goal, per-cloud unit economics, per-account roll-up |
| One per AWS account | Every service × the last 7 days, plus 7-day total and both deltas |
| One per GCP project | Same grid |
| `GMP (Maps)` | Per-API request volume, net cost, and cost per 1,000 requests |

Detail tabs are a **7-day grid**, not a today/yesterday pair: a single day-pair can't distinguish a real step change from ordinary weekday noise. A colour ramp across the day columns turns each tab into a heat map, so "climbing since Tuesday" and "spikes every Saturday" are visible without reading a number.

### Currency

Every cloud is rolled into one `report_currency` (INR). Cost Explorer only ever returns USD, so AWS is converted at `usd_inr_rate` — **pinned in config, never fetched live**. A cron whose headline moves because an FX API drifted overnight manufactures exactly the false anomaly this tool exists to suppress, and it would be unauditable after the fact. Detail tabs show both native and converted figures so an AWS tab can still be reconciled against the AWS console.

### Rides

Ride counts come from ClickHouse. The SQL is **not in this repository** — it lives in a directory supplied at deploy time (`ride_query_dir`, a ConfigMap in Kubernetes), because the queries embed internal schema and "what counts as a ride" is a business definition that should change without a code change.

Each `*.sql` file is one ride source; the filename becomes its label (a numeric ordering prefix is stripped, so `10-rides.sql` reports as "rides"). Sources are reported separately and summed. A query without a `{day}` placeholder is refused rather than run unbounded, and if any source fails the ride metrics are dropped entirely rather than reporting an understated denominator.

If ClickHouse is unreachable the run **degrades to cost-only** rather than failing — the cost data is worth sending on its own.

## ⚙️ Configuration

All settings come from **either** environment variables **or** `config.json`. Env wins. Same code path in dev and in-cluster.

| Key (json) | Env var | Default | Notes |
|---|---|---|---|
| `provider` | `PROVIDER` | `all` | `aws`, `gcp`, or `all` (the combined workbook) |
| `slack_bot_token` | `SLACK_BOT_TOKEN` | — | **Required.** `xoxb-…` with `chat:write` **and `files:write`** |
| `slack_channel_id` | `SLACK_CHANNEL_ID` | — | **Required.** Prefer the ID over `#name` |
| `aws_accounts` | `AWS_ACCOUNTS` | `[]` | **JSON array.** One tab per entry: `{"label", "role_arn"?, "profile"?, "region"?}`. Omit `role_arn` to use ambient credentials |
| `aws_region` | `AWS_REGION` | `ap-south-1` | Default region for CE clients |
| `gcp_billing_table` | `GCP_BILLING_TABLE` | — | **Required** for `gcp`/`all`. `project.dataset.table` |
| `gcp_project` | `GCP_PROJECT` | — | Project that runs/bills the BigQuery job |
| `gcp_projects` | `GCP_PROJECTS` | — | **Required** for `gcp`/`all`. Comma-separated, order preserved |
| `gmp_billing_table` | `GMP_BILLING_TABLE` | — | Maps billing export. Omit to skip the GMP tab |
| `gmp_projects` | `GMP_PROJECTS` | — | Comma-separated projects in the GMP billing account |
| `report_currency` | `REPORT_CURRENCY` | `INR` | Everything is converted into this before any arithmetic |
| `usd_inr_rate` | `USD_INR_RATE` | `88.0` | Pinned FX rate for AWS. Review it alongside the report |
| `clickhouse_host` | `CLICKHOUSE_HOST` | — | Omit to skip ride metrics entirely |
| `clickhouse_port` | `CLICKHOUSE_PORT` | `8123` | HTTP interface |
| `clickhouse_user` / `_password` | `CLICKHOUSE_USER` / `_PASSWORD` | — | Needs `SELECT` on the ride table only |
| `clickhouse_database` | `CLICKHOUSE_DATABASE` | `default` | Queries are fully qualified, so this rarely matters |
| `ride_query_dir` | `RIDE_QUERY_DIR` | — | Directory of `*.sql` ride queries. Unset skips ride metrics |
| `primary_ride_source` | `PRIMARY_RIDE_SOURCE` | first file | Which source is the base ride count; others are additive |
| `monthly_budgets` | `MONTHLY_BUDGETS` | `{}` | JSON, per cloud (`AWS`/`GCP`/`GMP`), in the reporting currency. **Fallback only** — see Budgets below |
| `projection_days` | `PROJECTION_DAYS` | `30` | Days used for the run-rate projection |
| `mention` | `MENTION` | empty | `here`, `channel`, user ID (`U…`), usergroup ID (`S…`) |
| `xyne_base_url` | `XYNE_BASE_URL` | — | Optional second destination. All three Xyne keys must be set or it is skipped |
| `xyne_jwt` | `XYNE_JWT` | — | App JWT. Needs `chat:write` and `files:write` |
| `xyne_channel` | `XYNE_CHANNEL` | — | Channel **name without** a leading `#` |
| `lookback_days` | `LOOKBACK_DAYS` | `21` | History pulled (must be ≥ 8 for the WoW column) |
| `vendor_logs_api_url` | `VENDOR_LOGS_API_URL` | — | Third-party verification vendor's daily request-logs endpoint. Omit to skip the vendor entirely |
| `vendor_app_id` / `vendor_app_key` | `VENDOR_APP_ID` / `VENDOR_APP_KEY` | — | Vendor API credentials (`appid` / `appKey` headers). Key is a secret |
| `vendor_pricing` | `VENDOR_PRICING` | `{}` | JSON, keyed by billing UNIT (not endpoint — see `vendor_billing._ENDPOINT_UNITS`); each value a list of `[lo, hi_or_null, price]` monthly-cumulative slab tiers, in `report_currency` |

### Budgets

Budgets live in ClickHouse (`cost_analytics.cost_budget`, DDL in `ddl/cost_budget.sql`) so finance
can change a number without a redeploy, and so this report and the Control Center cost dashboard
cannot disagree about the target. `MONTHLY_BUDGETS` stays as the fallback: if the table is
unreachable or empty, the configured value is used and the run carries on.

Two resolution rules:

* **Carry-forward** — a month with no row inherits the most recent earlier month, so a budget is
  entered once rather than re-entered monthly.
* **Cost-head level wins** — a row with an empty `account` is the budget for the whole cost head.
  Account-level rows exist for the dashboard's finer breakdown; this report falls back to summing
  them only when no cost-head row exists.

Change a budget by inserting a new row for that month — `ReplacingMergeTree` keeps the newest
`updated_at`:

```sql
INSERT INTO cost_analytics.cost_budget (month, type, cost_head, account, budget_inr, updated_by)
VALUES ('2026-10-01', 'Cloud', 'GCP Cost', '', 3200000, 'finance:<name>');
```

## 🔐 IAM

Minimal — read-only.

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Action": ["ce:GetCostAndUsage"],
    "Resource": "*"
  }]
}
```

Cost Explorer doesn't support resource-level ARNs, so `"Resource": "*"` is unavoidable — but the verb is read-only and only billing data leaves AWS.

> Already running a monitoring/observability service with `ce:*` access? Reuse its service account — `serviceAccountName: <yours>` and you're done.

**GCP** — read-only too. A dedicated service account needs exactly two grants:

- `roles/bigquery.jobUser` on the project that runs the query (to submit BigQuery jobs)
- `READER` on the billing-export **dataset** (least-privilege — not the whole project)

```bash
gcloud iam service-accounts create cost-anomaly-cron --project=<PROJECT>
gcloud projects add-iam-policy-binding <PROJECT> \
  --member="serviceAccount:cost-anomaly-cron@<PROJECT>.iam.gserviceaccount.com" \
  --role="roles/bigquery.jobUser"
# grant dataset READER via the dataset ACL (bq update --source), or a
# roles/bigquery.dataViewer binding on the dataset
```

In-cluster, bind it to the Kubernetes SA with **Workload Identity** (no keys):

```bash
gcloud iam service-accounts add-iam-policy-binding \
  cost-anomaly-cron@<PROJECT>.iam.gserviceaccount.com \
  --role roles/iam.workloadIdentityUser \
  --member "serviceAccount:<PROJECT>.svc.id.goog[<NAMESPACE>/cost-anomaly-cron]"
```


## 🚢 Deploy

```
.
├── k8s/                 # AWS manifests, public-friendly with <PLACEHOLDERS>
└── prod/                # gitignored — your real values live here
```

### 1) Push the image

```bash
aws ecr create-repository --repository-name cost-anomaly-cron --region <REGION>

docker buildx build --platform linux/amd64 -t cost-anomaly-cron:v1 . --load
docker tag cost-anomaly-cron:v1 <ACCOUNT>.dkr.ecr.<REGION>.amazonaws.com/cost-anomaly-cron:v1

aws ecr get-login-password --region <REGION> \
  | docker login --username AWS --password-stdin <ACCOUNT>.dkr.ecr.<REGION>.amazonaws.com
docker push <ACCOUNT>.dkr.ecr.<REGION>.amazonaws.com/cost-anomaly-cron:v1
```

### 2) Fill in your real manifests

```bash
cp k8s/cronjob.yaml prod/cronjob.yaml
cp k8s/secret.yaml  prod/secret.yaml
# edit both: namespace, serviceAccountName, image, channel ID, bot token
```

### 3) Apply

```bash
kubectl apply -f prod/secret.yaml
kubectl apply -f prod/cronjob.yaml
```

### 4) Smoke-test without waiting for the schedule

```bash
kubectl -n <NS> create job --from=cronjob/cost-anomaly-cron cost-anomaly-test-1
kubectl -n <NS> logs -f job/cost-anomaly-test-1
```

Look for either a Slack post or `No threshold crossings — skipping Slack post.` in the logs.

## 🕐 When does it run?

`0 17 * * *` in your business timezone. The report covers **T-2**, so by then every provider has finished restating that day — the numbers will not change afterwards.

## 🛠️ Architecture

| File | Role |
|---|---|
| `main.py` | Entrypoint. Collect → build workbook → post. `--dry-run` builds and prints without posting. |
| `collect.py` | Assembles every source into one cloud-agnostic report structure. Owns the T-2 target rule. |
| `providers/__init__.py` | Provider registry **and the contract**: `scopes`, `fetch_by_service`, `currency`. |
| `providers/aws.py` | Cost Explorer, **one CE client per account** via `sts:AssumeRole`. One tab per account. |
| `providers/gcp.py` | BigQuery billing export. Serves both the infra and GMP exports — identical schema, different table. |
| `rides.py` | ClickHouse ride counts over the HTTP interface, split by `cloud_type`. Degrades to `None` on failure. |
| `money.py` | Single reporting currency + pinned FX. The only place that converts. |
| `workbook.py` | XLSX builder: summary tab, per-account 7-day grids, GMP tab. |
| `slack.py` | Root message + threaded replies + workbook upload. |
| `xyne.py` | Second destination via a Slack-compatible adapter. Reuses `slack.py`'s tables and flattens them to text, so the two cannot drift. |
| `config.py` | Env-overrides-json loader with validation. Same code path local + prod. |

A **scope** is the unit each tab is built for: one AWS account, or one GCP project. `collect.py`, `workbook.py` and `slack.py` never learn which cloud a section came from.


## 🤝 Contributing

PRs welcome. The codebase is intentionally small and dependency-light. If you add a feature, please keep the **zero-noise-on-quiet-days** contract intact — over time it's the most important property.

## 📄 License

MIT.
