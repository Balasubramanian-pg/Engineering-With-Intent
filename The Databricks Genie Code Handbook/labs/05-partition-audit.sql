-- labs/05-partition-audit.sql
-- Audits whether queries on a large Delta table actually prune partitions,
-- versus silently falling back to a full table scan. Compares the CORRECT
-- form against three anti-patterns so the difference is visible in the plan.
--
-- ── What this audits ────────────────────────────────────────────────────
-- Partition pruning is decided by the physical plan, not by the SQL looking
-- "close enough." A query can filter on the partition column and still scan
-- every partition if the predicate is wrapped in a function, uses an
-- implicit cast, or references the wrong column. This file shows the four
-- cases side-by-side and tells you exactly what to grep for.
--
-- ── Reading the output ──────────────────────────────────────────────────
-- Run each `EXPLAIN FORMATTED` block and inspect the SCAN node. The string
-- you are looking for is `PartitionFilters:`. There are three possible
-- states, and they mean different things:
--
--   PartitionFilters: [isnotnull(event_date#...), (event_date#... = ...)]
--       PRUNED. The predicate was extracted as a partition filter and only
--       matching partitions will be read.
--
--   PartitionFilters: []
--       FULL SCAN. The scan node exists (table has partitions) but no
--       partition filter was extracted. Either the WHERE clause has no
--       partition predicate, or the predicate was wrapped in a function.
--
--   (no PartitionFilters: line at all)
--       NOT PARTITIONED. The scan is over a non-partitioned source, or the
--       table is partitioned on a different column than the one referenced.
--       Confirm with PART 0.
--
-- ── What EXPLAIN does and does not prove ────────────────────────────────
-- EXPLAIN does NOT execute the query — it only plans it. A plan that shows
-- `PartitionFilters: [...]` proves the OPTIMIZER *intends* to prune. To
-- confirm pruning actually saved work at runtime, check the query profile
-- (Databricks UI → Query → Query Profile → scan node → `numFiles` and
-- `numOutputBytes`) or `EXPLAIN COST` after `ANALYZE TABLE ... COMPUTE
-- STATISTICS`.
--
-- ── Assumptions (verify in PART 0) ──────────────────────────────────────
-- * retail_prod.gold.orders is partitioned, and event_date is the partition
--   column.
-- * event_date's data type is documented in PART 4 — the type trap only
--   fires when the partition column is stored as STRING.
--
-- ── Guarantees ──────────────────────────────────────────────────────────
-- * Idempotent — every statement is EXPLAIN or DESCRIBE; no DDL, no DML,
--                no temp tables, no session state. Re-running yields
--                identical plans (assuming unchanged table statistics).
-- * Stateless  — fully qualified names; no USE, no variables.
--
-- ── CI gate ─────────────────────────────────────────────────────────────
-- In a Databricks notebook or Python job, wrap PART 1's EXPLAIN in:
--
--     plan = spark.sql("EXPLAIN FORMATTED SELECT ...").first()[0]
--     assert "PartitionFilters: [isnotnull" in plan, \
--            "PART 1 (correct) must prune; got full scan"
--
-- and for PART 2 / PART 3:
--
--     assert "PartitionFilters: []" in plan or "PartitionFilters: [isnotnull" not in plan, \
--            "anti-pattern unexpectedly pruned — Spark behavior changed"


-- ─── PART 0: Confirm the table is partitioned on event_date ─────────────
-- If partitionColumns does not include event_date, no query below can prune
-- on it, and PART 1 will look like PART 2. Fix the table layout first.
DESCRIBE DETAIL retail_prod.gold.orders;
-- Look at the `partitionColumns` column. Expected: ['event_date'].
-- Also note `numFiles` and `sizeInBytes` — these are what the runtime
-- pruning saves you from scanning.


-- ─── PART 1: CORRECT — literal predicate on the partition column ────────
-- Pruned. The WHERE clause compares the partition column directly to a
-- literal, so the optimizer extracts it as a PartitionFilter.
--
-- Use a LITERAL date rather than CURRENT_DATE() - 1 for an audit: the plan
-- becomes deterministic (same every run), cacheable, and diffable across
-- CI runs. CURRENT_DATE() still prunes on a DATE column — see PART 5.
EXPLAIN FORMATTED
SELECT
    order_id,
    customer_id,
    net_amount
FROM retail_prod.gold.orders
WHERE event_date = DATE '2024-01-14';
-- EXPECT: PartitionFilters: [isnotnull(event_date#...), (event_date#... = 2024-01-14)]


-- ─── PART 2: ANTI-PATTERN 1 — no partition predicate ────────────────────
-- Full scan. The WHERE clause filters on a non-partition column, so no
-- partition filter can be extracted. This is EXPECTED full scan — it is
-- the baseline to recognize what a full scan looks like in the plan.
--
-- The lesson: if a query has no date predicate, it scans the whole table.
-- Add one (PART 1 form) whenever possible.
EXPLAIN FORMATTED
SELECT
    order_id,
    customer_id,
    net_amount
FROM retail_prod.gold.orders
WHERE net_amount > 100;
-- EXPECT: PartitionFilters: []
-- (or no PartitionFilters: line — the table's scan node still shows `Scan`
--  but with no partition filters pushed down.)


-- ─── PART 3: ANTI-PATTERN 2 — function wraps the partition column ──────
-- Likely non-pruned. `YEAR(event_date) = 2024` cannot be expressed as an
-- equality on `event_date` without knowing the date range, so the pruning
-- rule may or may not extract a range — Spark versions differ.
--
-- The RELIABLE form is a literal range (commented below the query).
EXPLAIN FORMATTED
SELECT
    order_id,
    customer_id,
    net_amount
FROM retail_prod.gold.orders
WHERE YEAR(event_date) = 2024 AND MONTH(event_date) = 1;
-- EXPECT: PartitionFilters: []  OR  a range PartitionFilters entry.
--         Spark 3.3+ often extracts a range; older versions do not.
--         Do not rely on either — use the literal range form below.

-- RELIABLE alternative to the anti-pattern above:
--
--     WHERE event_date >= DATE '2024-01-01'
--       AND event_date <  DATE '2024-02-01'
--
-- Range predicates on the raw partition column are unambiguously prunable.


-- ─── PART 4: TYPE TRAP — the one that silently bites ────────────────────
-- This is the case that makes the original query dangerous. If event_date
-- is a DATE column, both forms below prune. If event_date is a STRING
-- column (Hive-style partition `event_date=2024-01-14`), the DATE-vs-STRING
-- comparison triggers an implicit cast — `CAST(event_date AS DATE)` —
-- which wraps the partition attribute and CAN disable pruning.
--
-- Check the data type before running:
--
--     DESCRIBE retail_prod.gold.orders;
--     -- Look at the `data_type` column for `event_date`.
--
-- If event_date is DATE:   both forms below prune; use whichever is clearer.
-- If event_date is STRING: the first form below may not prune; the second
--                          form (string literal) is safe.

-- (a) DATE comparison. Prunes if event_date is DATE. May full-scan if
--     event_date is STRING (implicit cast wraps the attribute).
EXPLAIN FORMATTED
SELECT
    order_id,
    customer_id,
    net_amount
FROM retail_prod.gold.orders
WHERE event_date = CURRENT_DATE() - INTERVAL 1 DAY;

-- (b) String literal. Safe when event_date is STRING, prunable in both
--     cases because no cast wraps the partition attribute.
EXPLAIN FORMATTED
SELECT
    order_id,
    customer_id,
    net_amount
FROM retail_prod.gold.orders
WHERE event_date = date_format(CURRENT_DATE() - INTERVAL 1 DAY, 'yyyy-MM-dd');
-- EXPECT: PartitionFilters: [isnotnull(event_date#...), (event_date#... = ...)]
-- If (a) shows `PartitionFilters: []` and (b) shows a populated one, the
-- table's event_date is a STRING partition column — and the original
-- script's `CURRENT_DATE() - 1` form was a silent full scan.


-- ─── PART 5: CURRENT_DATE() caveat — works, but not for audits ──────────
-- `event_date = CURRENT_DATE() - 1` DOES prune on a DATE partition column.
-- It is a valid production pattern for "yesterday's data."
--
-- It is NOT a good pattern for THIS file, because:
--   1. The plan is not cacheable — Spark re-plans each run because
--      CURRENT_DATE() changes.
--   2. The plan is not diffable — the PartitionFilters value changes daily,
--      so CI comparing "plan equals the previous run's plan" fails by design.
--
-- Use the literal form in PART 1 for CI; use CURRENT_DATE() only in
-- production queries where the daily cutoff is the point.


-- ─── PART 6: Reading guide — what to check in every plan ────────────────
-- For each EXPLAIN FORMATTED output above, verify:
--
-- 1. The SCAN node has a `PartitionFilters:` line.
--    Absent → not partitioned (run PART 0).
-- 2. `PartitionFilters:` is non-empty (`[isnotnull(...), ...]`).
--    Empty `[]` → full scan.
-- 3. `PushedFilters:` (a separate line) may exist for non-partition
--    predicates (e.g. `net_amount > 100`). `PushedFilters` does NOT mean
--    partition pruning — it means row-group pushdown inside a scanned file.
-- 4. `numPartitions` (if shown) should be well below total partitions —
--    in the profile, `numFiles` and `numOutputBytes` confirm.
--
-- If PART 1 shows `PartitionFilters: []`, the table is not partitioned on
-- event_date — no amount of query rewriting will fix that. Re-layout the
-- table with `OPTIMIZE ... ZORDER` or partition by event_date.
