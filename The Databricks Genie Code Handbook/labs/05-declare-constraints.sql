-- labs/05-declare-constraints.sql
-- Declare informational PK/FK constraints in Unity Catalog to guide Genie Code
-- and enable RELY-based query optimizations on Photon-enabled compute.
--
-- ── What these constraints are (and are not) ──────────────────────────────
-- Primary key and foreign key constraints in Unity Catalog are INFORMATIONAL.
-- They are NOT ENFORCED: Databricks does not reject inserts that violate
-- them, and does not validate existing rows[reference:1]. You must ensure the
-- data satisfies the constraint yourself — via pipeline expectations, dbt
-- tests, or a manual audit.
--
-- ── What RELY buys you ────────────────────────────────────────────────────
-- RELY tells the optimizer it may TRUST the constraint to rewrite queries.
-- Concretely, with RELY on a primary key, Databricks can:
--   • drop a redundant DISTINCT on the key column
--   • eliminate a join to a table whose PK is already guaranteed unique
-- These optimizations require Photon-enabled compute (serverless or Pro
-- SQL warehouse); classic SQL warehouses ignore RELY[reference:2].
--
-- ⚠️  RELY is a promise. If the data violates the constraint, the optimizer's
--     rewrite is still applied, and the query result can be WRONG[reference:3].
--     Only add RELY after verifying uniqueness (e.g. `SELECT COUNT(*) =
--     COUNT(DISTINCT key)`). If you are not certain, use NORELY (the default)
--     and lose nothing except those optimizations.
--
-- ── Idempotency ───────────────────────────────────────────────────────────
-- Re-running this script must not fail. Databricks has no `ADD CONSTRAINT
-- IF NOT EXISTS`, so each section DROPs first. The FK is dropped before the
-- PK because a PK cannot be dropped while a referencing FK exists. Order:
--   1. DROP FK   (so the PK can be dropped)
--   2. DROP PK
--   3. ADD PK    (RELY)
--   4. ADD FK    (RELY)
-- Each DROP uses IF EXISTS, so the first run is a no-op for those steps.
--
-- ── Statelessness ─────────────────────────────────────────────────────────
-- Every object is fully qualified (catalog.schema.table). No session
-- variables, no `USE` statements, no reliance on current workspace state.
-- The script reads nothing and writes only the DDL below.

-- ─────────────────────────────────────────────────────────────────────────
-- 1. Drop the FK first (a PK cannot be dropped while a referencing FK exists)
-- ─────────────────────────────────────────────────────────────────────────
ALTER TABLE retail_prod.gold.orders
  DROP CONSTRAINT IF EXISTS fk_orders_customers;

-- ─────────────────────────────────────────────────────────────────────────
-- 2. Drop the existing PK (if any) — idempotent re-run support
-- ─────────────────────────────────────────────────────────────────────────
ALTER TABLE retail_prod.gold.customers
  DROP CONSTRAINT IF EXISTS pk_customers;

-- ─────────────────────────────────────────────────────────────────────────
-- 3. Add the PK with RELY
--    PK columns are implicitly NOT NULL, but the column should already be
--    declared NOT NULL in the table DDL. If it is nullable, ADD CONSTRAINT
--    fails with "Cannot create the primary key because its child column(s)
--    is nullable"[reference:4].
-- ─────────────────────────────────────────────────────────────────────────
ALTER TABLE retail_prod.gold.customers
  ADD CONSTRAINT pk_customers
  PRIMARY KEY (customer_id)
  NOT ENFORCED   -- informational only; no insert-time enforcement
  RELY;          -- optimizer may trust uniqueness for DISTINCT/join elimination

-- ─────────────────────────────────────────────────────────────────────────
-- 4. Add the FK with RELY
--    A foreign key MUST reference a PRIMARY KEY column (or, in preview, a
--    UNIQUE column) — not an arbitrary column[reference:5]. The referenced
--    PK above satisfies this. Defining the FK via ALTER TABLE (rather than
--    inline in CREATE TABLE) is the safe pattern for deployment[reference:6].
-- ─────────────────────────────────────────────────────────────────────────
ALTER TABLE retail_prod.gold.orders
  ADD CONSTRAINT fk_orders_customers
  FOREIGN KEY (customer_id)
  REFERENCES retail_prod.gold.customers(customer_id)
  NOT ENFORCED
  RELY;          -- enables join elimination to customers when the FK holds

-- ─────────────────────────────────────────────────────────────────────────
-- 5. Semantic metadata for Genie Code
--    COMMENT ON COLUMN is the modern syntax (Databricks Runtime 16.1+);
--    the legacy `ALTER TABLE ... CHANGE COLUMN ... COMMENT` is equivalent
--    but less readable[reference:7]. Synonyms in the comment help Genie map
--    natural-language questions ("topline", "turnover") to this column.
-- ─────────────────────────────────────────────────────────────────────────
COMMENT ON COLUMN retail_prod.gold.orders.net_amount IS
  'Net recognized revenue in USD after discounts and returns. '
  'Synonyms: sales, turnover, topline, revenue, net sales. '
  'Excludes tax and shipping. Grain: one row per order line.';

COMMENT ON TABLE retail_prod.gold.orders IS
  'Order-header + line grain. FK to customers.customer_id. '
  'net_amount is the additive revenue measure; order_id is the PK.';

COMMENT ON TABLE retail_prod.gold.customers IS
  'Customer dimension. PK: customer_id (surrogate). '
  'Use for filtering, grouping, and joining to orders.';
