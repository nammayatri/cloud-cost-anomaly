-- Monthly budgets, shared by this report and the Control Center cost dashboard.
--
-- Cluster ny_cluster is one shard with two replicas, so DDL needs ON CLUSTER —
-- a plain CREATE lands on a single node and the other replica never gets the
-- table. The engine path mirrors cost_daily exactly.
--
-- Editing a budget = INSERT a new row for that month. ReplacingMergeTree keeps
-- the newest updated_at. Never ALTER ... UPDATE.

CREATE TABLE IF NOT EXISTS cost_analytics.cost_budget ON CLUSTER ny_cluster
(
    `month`      Date,                               -- first day of the month
    `type`       LowCardinality(String),
    `cost_head`  LowCardinality(String) DEFAULT '',  -- '' = the whole type
    `account`    LowCardinality(String) DEFAULT '',  -- '' = every account in the head
    `budget_inr` Decimal(18, 2),
    `updated_by` String,
    `updated_at` DateTime DEFAULT now()
)
ENGINE = ReplicatedReplacingMergeTree('/clickhouse/tables/{shard}/cost_analytics/cost_budget', '{replica}', updated_at)
ORDER BY (month, type, cost_head, account)
SETTINGS index_granularity = 8192;

-- Seed from the MONTHLY_BUDGETS env the report shipped with. Dated 2026-06-01 so
-- it covers the whole history through carry-forward (a month with no row inherits
-- the most recent earlier month).
INSERT INTO cost_analytics.cost_budget (month, type, cost_head, account, budget_inr, updated_by) VALUES
    ('2026-06-01', 'Cloud',          'AWS Cost',   '', 1000000, 'seed:cron-env'),
    ('2026-06-01', 'Cloud',          'GCP Cost',   '', 3000000, 'seed:cron-env'),
    ('2026-06-01', 'Maps',           'Maps Cost',  '', 1500000, 'seed:cron-env'),
    ('2026-06-01', 'Data and Tools', 'Hyperverge', '',  300000, 'seed:cron-env');
