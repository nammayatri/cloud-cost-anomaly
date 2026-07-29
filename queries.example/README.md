# Ride-count queries

The real queries are **not** in this repository — they embed internal schema, and
what counts as "a ride" is a business definition that should be editable without a
code change. In Kubernetes they are supplied as a ConfigMap mounted at
`ride_query_dir` (default `/etc/cost-report/queries`).

Each `*.sql` file here is one ride SOURCE, executed in filename order. Prefix
files with a number to control that order — the prefix is stripped before display,
so `10-rides.sql` is reported as "rides". Sources appear both separately and
summed.

Contract:
  * include `{day}` where the target date goes — it is substituted as `YYYY-MM-DD`.
    A query without it is refused rather than run unbounded.
  * return `cloud <TAB> count`, or a single `count` column.
  * end with `FORMAT TabSeparated`.
