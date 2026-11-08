-- labs/05-fan-trap-test.sql
-- Demonstrates the fan trap: silent metric duplication when a parent table is
-- joined to a child without pre-aggregation. Compares the WRONG query
-- (direct join, then SUM) against the CORRECT query (CTE pre-aggregation,
-- then SUM), and provides an audit query that proves they diverge.
--
-- ── What a fan trap is ──────────────────────────────────────────────────
-- Join a parent (orders, 1 row per order) to a child (order_promotions,
-- N rows per order). The join emits one row per (order, promotion) pair.
-- An order with 3 promotions now appears 3 times. SUM(parent.measure) over
-- that row set is multiplied by 3.
--
-- The failure is SILENT. The query runs, returns numbers, nothing errors.
-- Only a deliberate comparison against the correct total catches it.
--
-- The trap compounds. Adding a SECOND child (e.g. order_payments, M rows
-- per order) yields N × M rows per order — SUM inflated by N × M. So
-- pre-aggregating ONE child is not enough; EVERY one-to-many join must be
-- pre-aggregated to the parent's grain.
--
-- ── Why CTE pre-aggregation fixes it ────────────────────────────────────
-- Aggregate each child to the parent's grain (GROUP BY the join key) BEFORE
-- joining. The parent LEFT JOIN one-row-per-key child yields exactly one
-- row per parent row, and SUM(parent.measure) is correct.
--
-- ── Assumptions (verify before trusting this file) ──────────────────────
-- * orders.order_id is unique. If not, even PART 1 fans out.
--     SELECT COUNT(*) - COUNT(DISTINCT order_id) AS duplicates
--     FROM retail_prod.gold.orders;        -- must be 0
-- * A row in order_promotions means "this promotion was applied to this
--   order". COUNT(*) is used (not COUNT(promotion_id)) because row
--   presence is the signal, and UC informational PKs do not enforce NOT
--   NULL on the child PK column.
--
-- ── Guarantees ──────────────────────────────────────────────────────────
-- * Idempotent — pure SELECT statements. No DDL, no temp tables, no
--                INSERT, no session state. Re-running yields identical
--                results.
-- * Stateless  — every reference is fully qualified (catalog.schema.table).
--                No USE, no variables, no reliance on current session.
--
-- ── CI gate ─────────────────────────────────────────────────────────────
-- Execute PART 3. It returns one row with a `verdict` column. Fail the job
-- if verdict starts with 'UNEXPECTED' (should be impossible — see PART 3).


-- ─── PART 1: CORRECT — per-order result (original query, preserved) ─────
-- Grain: one row per order. Safe to feed into any downstream
-- SUM(net_amount) or COUNT(*) without inflation.
WITH pre_aggregated_promotions AS (
    SELECT
        order_id,
        COUNT(*) AS promo_cnt
    FROM retail_prod.gold.order_promotions
    GROUP BY order_id
)
SELECT
    o.order_id,
    o.net_amount,
    COALESCE(p.promo_cnt, 0) AS applied_promotions
FROM retail_prod.gold.orders o
LEFT JOIN pre_aggregated_promotions p
  ON o.order_id = p.order_id;


-- ─── PART 2: Side-by-side — fan trap vs pre-aggregation ─────────────────
-- Both branches return the SAME columns. Read the `approach` label, then
-- compare `row_count` and `total_net_amount`. The WRONG branch's totals
-- will exceed the CORRECT branch's whenever any order has >1 promotion.
WITH correct_pre_agg AS (
    -- One row per order. SUM(net_amount) over this CTE is the true revenue.
    SELECT
        o.order_id,
        o.net_amount
    FROM retail_prod.gold.orders o
    LEFT JOIN (
        SELECT order_id
        FROM retail_prod.gold.order_promotions
        GROUP BY order_id
    ) p ON o.order_id = p.order_id
),
wrong_fan_trap AS (
    -- One row per (order, promotion) pair. SUM(net_amount) over this CTE
    -- is inflated by the per-order promotion count.
    SELECT
        o.order_id,
        o.net_amount,
        p.promotion_id
    FROM retail_prod.gold.orders o
    LEFT JOIN retail_prod.gold.order_promotions p
      ON o.order_id = p.order_id
)
SELECT
    'WRONG — direct join (fan trap)' AS approach,
    COUNT(*)                         AS row_count,
    COUNT(DISTINCT order_id)         AS distinct_orders,
    SUM(net_amount)                  AS total_net_amount
FROM wrong_fan_trap

UNION ALL

SELECT
    'CORRECT — pre-aggregated',
    COUNT(*),
    COUNT(DISTINCT order_id),
    SUM(net_amount)
FROM correct_pre_agg;


-- ─── PART 3: Audit — proves the trap is present (CI gate) ───────────────
-- Returns one row. `verdict` is the CI signal:
--
--   'FAN TRAP CONFIRMED...'   expected result whenever any order has >1
--                             promotion. The demo worked: direct join
--                             inflates SUM(net_amount).
--   'NO FAN-OUT DETECTED...'  not a failure of the query — the data is
--                             degenerate (every order has ≤1 promotion),
--                             so the demo has nothing to show. Seed the
--                             test data with at least one multi-promotion
--                             order to make the trap observable.
--   'UNEXPECTED...'           direct total < pre-agg total. Left-joining
--                             cannot remove rows, so this branch is a
--                             defensive guard against a broken assumption
--                             (e.g. NULL handling surprise). Fail CI.
WITH direct_total AS (
    SELECT SUM(o.net_amount) AS total
    FROM retail_prod.gold.orders o
    LEFT JOIN retail_prod.gold.order_promotions p
      ON o.order_id = p.order_id
),
safe_total AS (
    SELECT SUM(o.net_amount) AS total
    FROM retail_prod.gold.orders o
    LEFT JOIN (
        SELECT order_id
        FROM retail_prod.gold.order_promotions
        GROUP BY order_id
    ) p ON o.order_id = p.order_id
)
SELECT
    d.total                        AS wrong_total_fan_trap,
    s.total                        AS correct_total_pre_agg,
    d.total - s.total              AS inflation_delta,
    CASE
        WHEN ABS(d.total - s.total) < 0.01 THEN
            'NO FAN-OUT DETECTED — degenerate data (every order has ≤1 promotion)'
        WHEN d.total > s.total THEN
            'FAN TRAP CONFIRMED — direct join inflates SUM(net_amount)'
        ELSE
            'UNEXPECTED — direct total < pre-agg total; check orders uniqueness'
    END                            AS verdict
FROM direct_total d
CROSS JOIN safe_total s;
