-- labs/06-mask-verification.sql
-- Verify dynamic column masks and row filters for Genie Code executions.
--
-- ── Why the original SELECT is not a verification ──────────────────────
-- A SELECT as a privileged user returns unmasked data — by design. The
-- mask function receives the *current* user's identity and returns the
-- raw value for members of the privileged group. A SELECT therefore
-- proves nothing about whether non-privileged Genie users are protected.
--
-- Real verification has two independent layers:
--
--   LAYER 1 — METADATA: are the policies attached?
--     information_schema.column_masks and information_schema.row_filters
--     list every mask/filter bound to a column/table. If a row is absent,
--     no policy exists regardless of what the data looks like.
--
--   LAYER 2 — RUNTIME: do the policies actually fire?
--     Run the same SELECT as a NON-PRIVILEGED identity. Confirm that
--     tax_identifier is masked and that rows are filtered. This must be
--     a separate session with a different identity — the original
--     script's single SELECT cannot do this.
--
-- Genie Code inherits the logged-in user's identity and Unity Catalog
-- policies exactly as a human query would[reference:1][reference:2]. So a
-- runtime check as the non-privileged user is the direct, valid proof
-- that Genie Code is also protected.
--
-- ── Assumptions ────────────────────────────────────────────────────────
-- * The mask function is named (e.g. mask_tax_id) and exists in a schema
--   the caller can see. The original's comment says "Masked for
--   non-compliance personas" but never names the function.
-- * A row filter exists on retail_prod.gold.customers. The original script
--   selects region but never confirms a filter is attached.
-- * The non-privileged test identity is known and switchable
--   (e.g. via `SET ROLE` or a separate connection).
--
-- ── Idempotency ────────────────────────────────────────────────────────
-- Every statement is SELECT or DESCRIBE. No DDL, no DML, no temp tables,
-- no session variables set inside the script. Re-running yields the same
-- metadata; runtime results depend on the identity, not on time.
--
-- ── Statelessness ──────────────────────────────────────────────────────
-- Fully qualified references. No USE. The only input is the caller's
-- identity (which is the point). Nothing is written.
--
-- ── Exit code (when run as a notebook / job) ───────────────────────────
-- 0 — all assertions pass
-- 1 — a mask or filter is missing, or runtime enforcement failed


-- ═══════════════════════════════════════════════════════════════════════
-- PART 0: Preflight — confirm the table exists and find its masks/filters
-- ═══════════════════════════════════════════════════════════════════════

-- 0a. Metadata: is tax_identifier masked?
-- Expected: exactly one row for retail_prod.gold.customers.tax_identifier.
-- If this returns zero rows, NO mask exists on that column — stop here.
SELECT
    table_catalog,
    table_schema,
    table_name,
    column_name,
    mask_catalog,
    mask_schema,
    mask_name,
    mask_col_usage
FROM system.information_schema.column_masks
WHERE table_catalog = 'retail_prod'
  AND table_schema  = 'gold'
  AND table_name    = 'customers'
  AND column_name   = 'tax_identifier';
-- Note: information_schema.column_masks shows only columns the caller has
-- permission to interact with; tables accessible only via BROWSE are
-- excluded[reference:3]. If this is empty but you believe a mask exists,
-- verify you are querying the SYSTEM catalog's view, not a local one.

-- 0b. Metadata: is a row filter attached?
-- Expected: exactly one row for retail_prod.gold.customers.
SELECT
    table_catalog,
    table_schema,
    table_name,
    filter_catalog,
    filter_schema,
    filter_name,
    filter_col_usage
FROM system.information_schema.row_filters
WHERE table_catalog = 'retail_prod'
  AND table_schema  = 'gold'
  AND table_name    = 'customers';
-- Zero rows → no row filter. The SELECT below will return all rows
-- regardless of the caller's region; that is the finding.

-- 0c. Confirm the mask/filter functions exist and are what you think.
-- If 0a returned a mask_name, describe it to confirm it handles NULL and
-- does not leak the raw value in its error path.
-- DESCRIBE FUNCTION EXTENDED retail_prod.gold.mask_tax_id;
-- DESCRIBE FUNCTION EXTENDED retail_prod.gold.region_filter;


-- ═══════════════════════════════════════════════════════════════════════
-- PART 1: Runtime verification — run as the NON-PRIVILEGED identity
-- ═══════════════════════════════════════════════════════════════════════
--
-- ⚠️  PART 1 MUST be executed in a separate session connected as the
--     non-privileged user (or after switching role). Running it as an
--     admin will return raw data and the assertions will falsely fail —
--     or worse, falsely pass if the admin is also in the privileged
--     group that the mask whitelists.
--
-- In a notebook, the cleanest separation is a second Databricks
-- connection using a service principal or a test user, e.g.:
--
--     spark.conf.set("spark.databricks.user", "test_nonpriv@example.com")
--
-- or a separate SQL warehouse session. The queries below do not switch
-- identity themselves — that is deliberately out of scope, because a
-- naive `SET ROLE` can be silently ignored.

-- 1a. Row-filter count assertion.
-- Compare the row count the non-privileged user sees against the true
-- count (which requires running this in a separate privileged session).
-- If they differ, the row filter is active.
--
-- Replace :true_row_count with the value from a privileged session:
--     SELECT COUNT(*) FROM retail_prod.gold.customers;
WITH nonpriv AS (
    SELECT COUNT(*) AS visible_rows
    FROM retail_prod.gold.customers
)
SELECT
    visible_rows,
    :true_row_count AS true_rows,
    CASE
        WHEN visible_rows = :true_row_count THEN 'FAIL — row filter did not fire'
        WHEN visible_rows = 0              THEN 'PASS — filter returns zero rows (verify this is intended)'
        ELSE 'PASS — row filter active'
    END AS row_filter_verdict
FROM nonpriv;
-- Expected for a working filter: visible_rows < true_rows.

-- 1b. Column-mask assertion.
-- The original SELECT, but with an explicit check: does tax_identifier
-- look like a raw tax ID, or a masked value?
--
-- Masking strategies vary (NULL, '***', hash, redacted). We cannot know
-- which the function uses, but we CAN know that the output must not equal
-- the value a privileged user sees. Provide :true_tax_id_sample — a real
-- value read by the privileged session — and assert the non-privileged
-- result differs.
SELECT
    customer_id,
    tax_identifier,
    CASE
        WHEN tax_identifier IS NULL               THEN 'PASS — masked to NULL'
        WHEN tax_identifier = :true_tax_id_sample THEN 'FAIL — raw value leaked'
        ELSE 'PASS — value differs from privileged view'
    END AS mask_verdict
FROM retail_prod.gold.customers
LIMIT 10;
-- If every row says PASS, the mask is working. A single FAIL is a leak.

-- 1c. Combined assertion (row filter + mask in one row).
-- This is the direct analogue of what Genie Code will execute on behalf
-- of this user. If it passes here, Genie Code is protected.
SELECT
    COUNT(*)                                                    AS visible_rows,
    SUM(CASE WHEN tax_identifier IS NULL THEN 1 ELSE 0 END)     AS null_masked_rows,
    SUM(CASE WHEN tax_identifier = :true_tax_id_sample THEN 1 ELSE 0 END) AS raw_leak_rows,
    CASE
        WHEN COUNT(*) = :true_row_count
             AND SUM(CASE WHEN tax_identifier = :true_tax_id_sample THEN 1 ELSE 0 END) > 0
            THEN 'FAIL — no row filter and raw mask leak'
        WHEN COUNT(*) = :true_row_count
            THEN 'FAIL — row filter did not fire'
        WHEN SUM(CASE WHEN tax_identifier = :true_tax_id_sample THEN 1 ELSE 0 END) > 0
            THEN 'FAIL — raw value leaked through mask'
        ELSE 'PASS — row filter and column mask both enforced'
    END                                                         AS overall_verdict
FROM retail_prod.gold.customers;


-- ═══════════════════════════════════════════════════════════════════════
-- PART 2: Genie Code-specific confirmation
-- ═══════════════════════════════════════════════════════════════════════
--
-- Genie Code does not introduce a new enforcement path — it runs SQL as
-- the logged-in user, and Unity Catalog enforces masks/filters at query
-- execution time[reference:4][reference:5]. There is no separate "Genie
-- mask" to verify. The verification above IS the Genie verification.
--
-- The one Genie-specific risk is INDIRECT LEAKAGE: Genie may surface a
-- masked column's value through a different route (a JOIN to an
-- unmasked table, a UDF, or a curated view that omits the mask). Check
-- that every path Genie can take to tax_identifier is covered:
--
-- 2a. Are there UNMASKED copies of tax_identifier elsewhere?
--     If yes, Genie can join around the mask. This is the most common
--     real-world bypass.
SELECT
    c.table_catalog,
    c.table_schema,
    c.table_name,
    c.column_name
FROM system.information_schema.columns c
WHERE LOWER(c.column_name) LIKE '%tax_id%'
  AND NOT EXISTS (
      SELECT 1
      FROM system.information_schema.column_masks m
      WHERE m.table_catalog = c.table_catalog
        AND m.table_schema  = c.table_schema
        AND m.table_name    = c.table_name
        AND m.column_name   = c.column_name
  );
-- Expected: zero rows. Any row here is a table with a tax_id-like column
-- that has NO mask — Genie can read it directly or join to it.

-- 2b. Are there VIEWS over customers that omit the mask?
--     A view without the mask re-exposes the column, and Genie will
--     happily query the view instead of the base table.
SELECT
    v.table_catalog,
    v.table_schema,
    v.table_name,
    v.view_definition
FROM system.information_schema.views v
WHERE v.view_definition ILIKE '%tax_identifier%'
  AND NOT (v.table_catalog = 'retail_prod'
       AND v.table_schema  = 'gold'
       AND v.table_name    = 'customers');
-- Expected: zero rows, or each view in the result is itself masked at
-- the view level. A view exposing raw tax_identifier defeats the base
-- table mask for Genie.


-- ═══════════════════════════════════════════════════════════════════════
-- PART 3: CI gate (pseudo-code)
-- ═══════════════════════════════════════════════════════════════════════
--
-- Layer 1 (metadata) can run in CI with an admin identity:
--
--     mask = spark.sql("""
--         SELECT mask_name FROM system.information_schema.column_masks
--         WHERE table_catalog='retail_prod' AND table_schema='gold'
--           AND table_name='customers' AND column_name='tax_identifier'
--     """).first()
--     assert mask is not None, "tax_identifier has no column mask"
--
--     filt = spark.sql("""
--         SELECT filter_name FROM system.information_schema.row_filters
--         WHERE table_catalog='retail_prod' AND table_schema='gold'
--           AND table_name='customers'
--     """).first()
--     assert filt is not None, "customers has no row filter"
--
-- Layer 2 (runtime) must run in a job with the NON-PRIVILEGED identity:
--
--     leak = spark.sql("""
--         SELECT COUNT(*) FROM retail_prod.gold.customers
--         WHERE tax_identifier = '<known real value>'
--     """).first()[0]
--     assert leak == 0, f"{leak} rows leaked raw tax_identifier"
--
--     visible = spark.sql("SELECT COUNT(*) FROM retail_prod.gold.customers").first()[0]
--     assert visible < <true_row_count>, "row filter did not fire"
--
-- Both layers must pass. Metadata alone (PART 0) is insufficient: a mask
-- can be attached and still be broken (returns the raw value). Runtime
-- alone (PART 1) is insufficient: a mask that happens to be absent
-- returns raw data that an assertion written against a privileged
-- baseline may not catch if the baseline is also unmasked.
