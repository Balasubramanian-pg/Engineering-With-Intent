-- labs/06-curate-gold-views.sql
-- Create the AI-facing curated schema and the daily revenue view, with
-- explicit grain, preserved identity, revenue-preserving joins, and honest
-- currency semantics.
--
-- ── What "curated" means here ───────────────────────────────────────────
-- A curated view is a CONTRACT. Genie and any downstream consumer will
-- trust its grain, its column meanings, and its completeness. A silent
-- change to any of the three (a new SELECT column under GROUP BY ALL, a
-- join switched from INNER to LEFT, a renamed unit) is a production
-- incident. The choices below are documented so they don't drift.
--
-- ── What the original version got wrong (documented so it doesn't
--    regress; each is a real data bug, not style) ───────────────────────
-- 1. GROUP BY ALL — grain changes silently when the SELECT changes.
--    Fixed: explicit GROUP BY (customer_id, customer_name, region,
--    event_date). Adding a column to the SELECT now requires adding it
--    here — a conscious edit, not an accidental one.
-- 2. GROUP BY customer_name (not customer_id) — two customers with the
--    same name in the same region are merged; their revenue is summed
--    into one row. Fixed: customer_id is in the SELECT and the GROUP BY.
--    The name is a label, not a key.
-- 3. INNER JOIN — guest checkouts, NULL customer_id, and orphans left by
--    the NOT ENFORCED FK are dropped. A view called "daily revenue"
--    whose total is less than the true total is worse than no view.
--    Fixed: LEFT JOIN + COALESCE to '<unattributed>'. The unattributed
--    revenue is visible as its own row, not silently lost.
-- 4. total_revenue_usd — names a unit the query doesn't compute. Fixed:
--    the column is named total_net_amount, with the currency stated in
--    the column comment. If your upstream is already USD-converted, the
--    PART 0c check will confirm it and you can rename to _usd — with
--    the check, not an assumption.
--
-- ── Assumptions to verify before running (PART 0) ──────────────────────
-- * orders.customer_id is nullable OR can reference a nonexistent
--   customers.customer_id — both true given a NOT ENFORCED FK.
-- * customers.customer_id is unique (either by PK or by data cleanliness).
-- * net_amount's unit is WAERK (document currency), not already USD.
-- * orders is at header or item grain — verify in PART 0d, because
--   SUM(net_amount) means different things at each grain.
--
-- ── Idempotency ─────────────────────────────────────────────────────────
-- * CREATE SCHEMA IF NOT EXISTS — no-op when the schema exists.
-- * CREATE OR REPLACE VIEW — SET semantics on the view definition. With
--   fixed column names and types, re-running yields an identical view.
--
-- ── Statelessness ───────────────────────────────────────────────────────
-- Every object fully qualified. No USE, no temp tables, no session
-- variables. Reads only the two source tables; writes only the schema
-- (once) and the view.
--
-- ── Caveats that the view does NOT solve ────────────────────────────────
-- * Performance: the view has no WHERE clause — scanning it scans the
--   whole orders table. Downstream queries must filter on event_date to
--   prune partitions (see 05-partition-audit.sql).
-- * PII: customer_name is in this view. If the view is exposed to Genie
--   for non-privileged users, mask or omit customer_name (PII governance
--   is outside this file; add a separate PII-safe view if needed).
-- * Currency: cross-currency SUM is still wrong. The column comment says
--   so. Adding a currency column and grouping by it is the next step if
--   your consumers need cross-currency aggregation (see PART 3).


-- ─── PART 0: Pre-flight — confirm assumptions ─────────────────────────

-- 0a. customers.customer_id must be unique. If not, INNER/LATER JOIN fans
--     out order revenue.
SELECT
    COUNT(*) - COUNT(DISTINCT customer_id) AS duplicate_customers
FROM retail_prod.gold.customers;
-- Expected: 0. Non-zero → fix the dimension before creating the view.

-- 0b. How much revenue is excluded by an INNER JOIN?
--     This is the number the original view silently hides.
SELECT
    SUM(o.net_amount) AS total_net_amount,
    SUM(CASE WHEN c.customer_id IS NULL THEN o.net_amount ELSE 0 END) AS unattributed,
    ROUND(100.0 * SUM(CASE WHEN c.customer_id IS NULL THEN o.net_amount ELSE 0 END)
          / NULLIF(SUM(o.net_amount), 0), 4) AS unattributed_pct
FROM retail_prod.gold.orders o
LEFT JOIN retail_prod.gold.customers c
  ON o.customer_id = c.customer_id;
-- unattributed_pct is the revenue the original view was dropping.
-- Small (<0.1%): the LEFT JOIN below still matters (avoids regression).
-- Large (>1%): the INNER JOIN was a material bug; note it and tell the
--              data owner.

-- 0c. Confirm net_amount's currency. Is there a currency column?
DESCRIBE TABLE retail_prod.gold.orders;
-- Look for a column named waerk / currency / curr.
-- Present → PART 1 must group by it (see PART 3 for the two-column version).
-- Absent  → net_amount is (assumed) already in one currency; the PART 1
--           comment should state which, explicitly. If the assumption
--           cannot be confirmed, do not name the output _usd.

-- 0d. Confirm grain of orders — header vs item.
SELECT
    COUNT(*)                        AS row_count,
    COUNT(DISTINCT order_id)        AS distinct_docs,
    COUNT(DISTINCT CONCAT_WS('-', order_id, 1)) AS distinct_items_dummy
FROM retail_prod.gold.orders;
-- row_count == distinct_docs → header grain; SUM(net_amount) over the
--                              whole table is the total.
-- row_count >  distinct_docs → item grain or duplicates; SUM(net_amount)
--                              per (customer, date) is still correct, but
--                              a downstream join to another item table
--                              fans out (see 05-fan-trap-test.sql).


-- ─── PART 1: Schema + corrected view ──────────────────────────────────

CREATE SCHEMA IF NOT EXISTS retail_prod.gold_curated
  COMMENT 'AI-facing curated layer. Views here are CONTRACTS: grain, '
          'column meanings, and completeness are documented and must not '
          'change without consumer notification.';

CREATE OR REPLACE VIEW retail_prod.gold_curated.daily_revenue_by_customer (
    customer_id,     COMMENT 'Customer surrogate key. NULL for revenue '
                             'that cannot be attributed to a known customer '
                             '(guest checkouts, orphans from NOT ENFORCED FK).',
    customer_name,   COMMENT 'Display label. NOT unique — two customers can '
                             'share a name. Use customer_id for identity. '
                             'COALESCE to ''<unattributed>'' when customer_id '
                             'is NULL. Contains PII; mask before exposing to '
                             'non-privileged consumers.',
    region,          COMMENT 'Customer region. NULL for unattributed revenue.',
    event_date,      COMMENT 'Order event date (source: orders.event_date). '
                             'One row per (customer, date). Filter on this '
                             'column to prune partitions in the source table.',
    total_net_amount COMMENT 'Sum of orders.net_amount for this (customer, '
                             'date). Unit: document currency (WAERK) unless '
                             'the source table has been USD-converted — '
                             'verify upstream. Do not SUM across currencies '
                             'in downstream queries without conversion.'
)
COMMENT 'Daily net order amount by customer. Grain: one row per '
        '(customer_id, event_date). Unattributed revenue is preserved as a '
        'row with customer_id IS NULL and customer_name = ''<unattributed>'' '
        'so that SUM(total_net_amount) over the whole view equals the true '
        'total in orders. Do not change the grain or join type without '
        'notifying consumers.'
AS
SELECT
    o.customer_id                              AS customer_id,
    COALESCE(c.customer_name, '<unattributed>') AS customer_name,
    c.region                                    AS region,
    o.event_date                                AS event_date,
    SUM(o.net_amount)                           AS total_net_amount
FROM retail_prod.gold.orders o
LEFT JOIN retail_prod.gold.customers c
       ON o.customer_id = c.customer_id
-- Explicit GROUP BY. Never GROUP BY ALL — it silently changes the grain
-- when a column is added to the SELECT, and CREATE OR REPLACE VIEW makes
-- that change invisible to consumers. Every new key column requires a
-- conscious edit here, which is the point.
GROUP BY
    o.customer_id,
    COALESCE(c.customer_name, '<unattributed>'),
    c.region,
    o.event_date;


-- ─── PART 2: Verification — assert the contract ────────────────────────

-- 2a. Grain assertion: (customer_id, event_date) must be unique.
--     NULL customer_id is a legitimate value (unattributed); include it.
SELECT COUNT(*) - COUNT(DISTINCT CONCAT_WS('|',
           COALESCE(CAST(customer_id AS STRING), '<null>'),
           CAST(event_date AS STRING)
       )) AS duplicate_keys
FROM retail_prod.gold_curated.daily_revenue_by_customer;
-- Expected: 0. Non-zero → the view is fanning out (source duplicate, or
-- the join unexpectedly produced multiple rows per key).

-- 2b. Revenue-preservation assertion: the sum over the view must equal the
--     sum over the source. This is the check that the original INNER JOIN
--     would have failed.
SELECT
    (SELECT SUM(net_amount)                    FROM retail_prod.gold.orders)
        AS source_total,
    (SELECT SUM(total_net_amount)
       FROM retail_prod.gold_curated.daily_revenue_by_customer) AS view_total;
-- Expected: source_total = view_total (within rounding). A shortfall
-- means the LEFT JOIN didn't preserve all rows — investigate before
-- announcing the view.

-- 2c. Unattributed bucket — confirm it exists and is reported, not hidden.
SELECT
    customer_id,
    customer_name,
    SUM(total_net_amount) AS unattributed_total
FROM retail_prod.gold_curated.daily_revenue_by_customer
WHERE customer_id IS NULL
GROUP BY customer_id, customer_name;
-- Expected (in a dirty dataset): one row with
-- customer_name = '<unattributed>' and a non-zero total. If zero, no
-- orphan orders exist — good, but the LEFT JOIN stays for safety.

-- 2d. CI gate (pseudo-code for a Databricks notebook / job):
--
--     dups = spark.sql("""
--         SELECT COUNT(*) - COUNT(DISTINCT CONCAT_WS('|',
--                    COALESCE(CAST(customer_id AS STRING), '<null>'),
--                    CAST(event_date AS STRING)))
--         FROM retail_prod.gold_curated.daily_revenue_by_customer
--     """).first()[0]
--     assert dups == 0, f"grain violation: {dups} duplicate (customer_id, event_date) keys"
--
--     source_total, view_total = spark.sql("""
--         SELECT
--             (SELECT SUM(net_amount) FROM retail_prod.gold.orders),
--             (SELECT SUM(total_net_amount)
--                FROM retail_prod.gold_curated.daily_revenue_by_customer)
--     """).first()
--     assert abs(source_total - view_total) < 0.01, \
--            f"revenue lost: source={source_total}, view={view_total}"


-- ─── PART 3: If you need cross-currency or a real _usd suffix ─────────
--
-- If PART 0c shows a currency column (waerk) exists, and consumers need
-- to aggregate across currencies, add it to the view's grain and rename:
--
--   CREATE OR REPLACE VIEW retail_prod.gold_curated.daily_revenue_by_customer_by_currency
--   AS
--   SELECT
--       o.customer_id,
--       COALESCE(c.customer_name, '<unattributed>') AS customer_name,
--       c.region,
--       o.event_date,
--       o.waerk                                     AS currency,
--       SUM(o.net_amount)                           AS total_net_amount
--   FROM retail_prod.gold.orders o
--   LEFT JOIN retail_prod.gold.customers c
--          ON o.customer_id = c.customer_id
--   GROUP BY o.customer_id, COALESCE(c.customer_name, '<unattributed>'),
--            c.region, o.event_date, o.waerk;
--
-- Now a `_usd` column is a real fix — join an FX table and convert:
--
--   ... SUM(o.net_amount * COALESCE(fx.rate_to_usd, 1.0)) AS total_net_amount_usd
--   ... LEFT JOIN retail_prod.gold.fx_rates fx
--              ON  fx.from_currency = o.waerk
--              AND fx.to_currency   = 'USD'
--              AND fx.rate_date     = o.event_date
--
-- Do not name a column _usd without doing this. Genie will repeat the
-- unit claim to every user who asks "what was revenue last month?"
