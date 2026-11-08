-- labs/05-synonym-annotator.sql
-- Annotate SAP/ERP columns in Unity Catalog with business synonyms so that
-- Genie Code (and other NL→SQL consumers) can translate user language into
-- correct queries.
--
-- ── Why this file exists ────────────────────────────────────────────────
-- Genie reads column comments as authoritative when it resolves a user
-- term to a column. A wrong synonym is worse than no synonym: it causes
-- Genie to confidently generate the wrong query. The original comments in
-- this file contained six active bugs — Invoice ≠ Sales Order, Revenue ≠
-- Net Value, Client ≠ Customer, plus silent currency/grain/leading-zero
-- traps. Every one is fixed below, and the reasoning is documented so it
-- does not regress.
--
-- ── Assumptions to verify before running (PART 0) ───────────────────────
-- * Table is at ONE grain (header VBAK or item VBAP). The comment for
--   netwr changes meaning depending on which.
-- * Currency column (WAERK or equivalent) exists, and its values are
--   informative (single currency vs mixed).
-- * vbeln / kunnr are stored as strings with leading zeros, not INT.
--
-- ── Idempotency ─────────────────────────────────────────────────────────
-- COMMENT ON COLUMN is SET semantics: re-running overwrites the comment
-- with the same static string. The end state is identical after N runs.
-- Every comment below is a literal — none includes NOW() or any value that
-- would change between runs, which is what makes this truly idempotent.
--
-- ── Statelessness ───────────────────────────────────────────────────────
-- Every object is fully qualified (catalog.schema.table.column). No USE
-- statement, no session variables, no reliance on current catalog/schema.
-- Reads nothing; writes only column comments.
--
-- ── Runtime compatibility ───────────────────────────────────────────────
-- `COMMENT ON COLUMN` requires DBR 16.1+ / serverless SQL. On older
-- runtimes, replace each statement with:
--     ALTER TABLE <table> ALTER COLUMN <col> COMMENT '<comment>';
-- Both forms produce identical metadata and are equally idempotent.
--
-- ── Verification ────────────────────────────────────────────────────────
-- See PART 2. A CI gate that requires each comment length > 50 chars and
-- the literal substring "Synonyms:" catches accidental deletion or
-- truncation.


-- ─── PART 0: Pre-flight — confirm assumptions ───────────────────────────

-- 0a. Verify all three columns exist and note their types.
--     If vbeln or kunnr is INT, the leading-zero warning in the comments
--     below is moot — but if it is STRING, it is critical.
DESCRIBE TABLE retail_prod.gold.orders;
-- Look at data_type for vbeln, kunnr, netwr. Expected: STRING, STRING, DECIMAL.

-- 0b. Confirm leading zeros are present (STRING columns only).
--     If this returns a row, filters like vbeln = '1' will fail.
SELECT vbeln, kunnr
FROM retail_prod.gold.orders
WHERE vbeln LIKE '0%' OR kunnr LIKE '0%'
LIMIT 1;

-- 0c. Confirm grain — header vs item. This determines whether SUM(netwr)
--     is meaningful as-is, or already pre-summed.
SELECT
    COUNT(*)                                     AS row_count,
    COUNT(DISTINCT vbeln)                        AS distinct_docs,
    COUNT(DISTINCT CONCAT_WS('-', vbeln, posnr)) AS distinct_items
FROM retail_prod.gold.orders;
-- row_count = distinct_docs  → header grain (VBAK); SUM(netwr) is the
--                              total across the whole table, correctly.
-- row_count = distinct_items → item grain (VBAP); SUM(netwr) is correct,
--                              but joining to another item table (e.g.
--                              schedules) will fan out — see 05-fan-trap-test.sql.
-- row_count ≠ either          → investigate; the table is denormalized or
--                              has duplicates.

-- 0d. Confirm currency. If waerk does not exist, SUM(netwr) is only
--     meaningful if you know (from outside this file) that all rows are in
--     one currency — say so in the comment rather than assuming it.
SELECT waerk, COUNT(*) AS row_count
FROM retail_prod.gold.orders
GROUP BY waerk
ORDER BY row_count DESC;
-- One row → single currency; the comment below can say which.
-- Multiple rows → cross-currency; the comment below MUST warn.
-- Zero rows / column missing → adjust the netwr comment to state the
--                              currency assumption explicitly.


-- ─── PART 1: Corrected comments ────────────────────────────────────────
--
-- Design rules followed (documented so they don't regress):
--   * Synonyms are REAL user vocabulary, not aspirational accounting terms.
--   * Synonyms that could mean a DIFFERENT column are omitted. ("Invoice",
--     "Revenue", "Client" are omitted — see PART 3 for why.)
--   * Where a synonym could apply at multiple grains, the grain is stated.
--   * Currency and unit are stated when they affect the meaning of an
--     aggregate.
--   * Storage format (leading zeros, string vs int) is stated when it
--     affects a filter.
--   * Each comment is under ~250 chars, well inside the ~1024-char limit.

COMMENT ON COLUMN retail_prod.gold.orders.vbeln IS
'SAP sales document number. Synonyms: Sales Order Number, Order Number, '
'SO Number, Sales Doc. Unique within a document type — check VBTYP for '
'document category. Stored as 10-char string with leading zeros '
'(e.g. 0000012345); filter as string, not integer.';

COMMENT ON COLUMN retail_prod.gold.orders.kunnr IS
'SAP customer account number. Synonyms: Customer ID, Customer Number, '
'Sold-To Party. Stored as 10-char string with leading zeros '
'(e.g. 0000012345); filter as string. Not the SAP client/tenant — that is '
'MANDT and is not exposed in this table.';

COMMENT ON COLUMN retail_prod.gold.orders.netwr IS
'Net order value in document currency (column WAERK). Synonyms: Net Value, '
'Order Value, Net Amount. NOT recognized revenue (see finance ledger) and '
'NOT tax-inclusive gross. Filter by currency before summing — do not SUM '
'across currencies.';

-- Table-level comment: reinforces grain and column semantics for Genie,
-- which reads both column and table comments.
COMMENT ON TABLE retail_prod.gold.orders IS
'SAP sales document header. PK: vbeln (per document type). '
'One row per sales order. netwr is the order value in document currency; '
'join to line items by vbeln for detail. Fan-trap risk when joining to '
'one-to-many children (promotions, payments) without pre-aggregation.';


-- ─── PART 2: Verification ──────────────────────────────────────────────
-- Confirm the comments landed and contain the expected markers.

SELECT
    column_name,
    data_type,
    comment,
    LENGTH(comment) AS comment_len,
    comment LIKE '%Synonyms:%' AS has_synonyms_marker
FROM system.information_schema.columns
WHERE table_catalog = 'retail_prod'
  AND table_schema  = 'gold'
  AND table_name    = 'orders'
  AND column_name IN ('vbeln', 'kunnr', 'netwr')
ORDER BY column_name;
-- Expected: three rows, each with comment_len > 100 and
-- has_synonyms_marker = true.

-- CI gate (pseudo-code for a Databricks notebook / job):
--
--     for col in ("vbeln", "kunnr", "netwr"):
--         row = spark.sql(f"""
--             SELECT comment FROM system.information_schema.columns
--             WHERE table_catalog = 'retail_prod'
--               AND table_schema  = 'gold'
--               AND table_name    = 'orders'
--               AND column_name   = '{col}'
--         """).first()
--         assert row and row.comment, f"{col}: comment missing"
--         assert len(row.comment) > 50, f"{col}: comment truncated"
--         assert "Synonyms:" in row.comment, f"{col}: synonyms marker lost"


-- ─── PART 3: What NOT to put in a synonym list ────────────────────────
--
-- Documenting the bugs the original version contained, so the pattern is
-- recognized and not repeated:
--
-- ✗  "Invoice" (for vbeln)
--    VBELN of document type M is an invoice, but VBELN in VBAK is a sales
--    order. Two different tables, two different VBELN values for "the same
--    invoice." Genie will sum order value into invoiced revenue.
--
-- ✗  "Topline Revenue" (for netwr)
--    NETWR is booked order value in document currency. Recognized revenue
--    lives in the finance ledger, in a different accounting period, after
--    billing. Calling them synonyms makes Genie answer "what was Q3
--    revenue?" with order value.
--
-- ✗  "Client Code" (for kunnr)
--    In SAP, "client" = the tenant (MANDT). Customer ≠ client. Genie will
--    conflate the two and filter on the wrong dimension.
--
-- ✗  "Total Amount" (for netwr)
--    Total of what? Line item, order, or tax-inclusive invoice? Without
--    grain, the synonym is unresolvable. "Net Value" is precise because
--    "net" excludes tax and "value" doesn't presume grain.
--
-- ✗  "Order ID" (for vbeln)
--    Fine IF there is exactly one order concept in the schema. Not fine if
--    there is also a purchase_order_id, return_order_id, or
--    delivery_document_id — Genie will match the wrong one.
--
-- The rule: a synonym should be a term that (a) a real user would type and
-- (b) unambiguously resolves to THIS column and no other. If (b) is not
-- guaranteed, omit the synonym rather than include it — a wrong match is
-- worse than a miss.
