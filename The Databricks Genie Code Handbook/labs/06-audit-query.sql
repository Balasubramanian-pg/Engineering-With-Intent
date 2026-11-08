-- labs/06-audit-query.sql
-- Audit Genie-originated SQL in system.query.history.
--
-- ── What this audits (and what it does not) ─────────────────────────────
-- Population: statements in system.query.history whose query_source struct
-- carries a genie_space_id, PLUS statements whose client_application equals
-- 'Databricks SQL Genie Space'. Both signals are checked because:
--   * client_application is a free-text field "derived from information
--     provided by client applications" and "cannot be guaranteed" stable
--     over time [3].
--   * query_source.genie_space_id is structured and populated by Databricks.
-- A statement is counted if EITHER signal is present — a conservative
-- union, so a query is not lost if one signal is missing.
--
-- NOT covered by this table: Genie Code *conversations* and inline-assistant
-- interactions. Those live in system.access.assistant_events [4]. If the
-- question is "how many people talked to Genie," query that table. If the
-- question is "what SQL did Genie run and what did it cost," use this file.
-- A Databricks community answer notes that Genie Space queries may not be
-- logged in query.history at all in some configurations [5]; verify with
-- PART 0 before treating an empty result as "no Genie usage."
--
-- ── The statement_text trap ─────────────────────────────────────────────
-- statement_text returns '<REDACTED>' by default. Only account admins and
-- members of databricks_pii_access read raw text [1][2]. This file does
-- NOT select raw statement_text. It selects a HASH of it (stable, non-PII)
-- and a length, which are sufficient for:
--   * deduplicating identical repeated queries (hash grouping)
--   * spotting query-shape patterns (length, statement_type)
--   * correlating with a known query by hash, computed elsewhere under PII
--     access, without exposing the text in the audit output.
-- If you are an account admin and need raw text, uncomment PART 4.
--
-- ── Idempotency ─────────────────────────────────────────────────────────
-- The window is bound to TWO fixed timestamps passed as named parameters,
-- not CURRENT_TIMESTAMP(). Re-running with the same parameters yields the
-- same result set. CURRENT_TIMESTAMP() would make every run return a
-- different population — the opposite of an audit.
--
-- In Databricks SQL, set them at the top of the notebook/query:
--     SET VAR audit_start = '2025-01-01 00:00:00';
--     SET VAR audit_end   = '2025-02-01 00:00:00';
-- Or in a Python job, bind them via parameter markers (:audit_start,
-- :audit_end). Both forms are supported; the query below uses `:name`
-- markers so it runs unchanged from SQL, Python, and the Jobs API.
--
-- ── Statelessness ───────────────────────────────────────────────────────
-- Every reference is fully qualified (system.query.history). No USE, no
-- temp tables, no session variables set inside the query, no writes. The
-- only inputs are the two bound parameters.
--
-- ── CI gate ─────────────────────────────────────────────────────────────
-- PART 1 returns a single row with a `health` column. Fail the job if
-- health = 'DEGRADED' (failure_rate > 5% OR p95_duration_ms > 60_000).


-- ─── PART 0: Verify the population is non-empty before auditing ─────────
-- If this returns zero rows, PART 1–3 will report "no data" — which looks
-- like a clean audit but actually means the population is absent (wrong
-- window, wrong workspace, Genie not in query.history for this account).
-- Do not conflate "no Genie usage" with "Genie not visible here."
SELECT
    client_application,
    query_source.genie_space_id IS NOT NULL AS has_genie_space_id,
    COUNT(*) AS statement_count
FROM system.query.history
WHERE start_time >= :audit_start
  AND start_time <  :audit_end
  AND (
        client_application = 'Databricks SQL Genie Space'
     OR query_source.genie_space_id IS NOT NULL
  )
GROUP BY 1, 2
ORDER BY statement_count DESC;
-- Expected: at least one row with statement_count > 0.
-- Zero rows → widen the window, confirm the workspace is in the same
-- region as the account, or use system.access.assistant_events instead.


-- ─── PART 1: Headline audit — one row, CI-gateable ─────────────────────
-- The single-row summary an audit should lead with. Every column is
-- computed from the same filtered population; no sampling, no LIMIT.
WITH genie_queries AS (
    SELECT
        statement_id,
        executed_by,
        execution_status,
        total_duration_ms,
        execution_duration_ms,
        waiting_for_compute_duration_ms,
        read_bytes,
        read_rows,
        produced_rows,
        statement_type,
        query_source.genie_space_id,
        client_application,
        error_message
    FROM system.query.history
    WHERE start_time >= :audit_start
      AND start_time <  :audit_end
      AND (
            client_application = 'Databricks SQL Genie Space'
         OR query_source.genie_space_id IS NOT NULL
      )
)
SELECT
    COUNT(*)                                                       AS total_statements,
    COUNT(DISTINCT executed_by)                                    AS distinct_users,
    COUNT(DISTINCT query_source.genie_space_id)                    AS distinct_genie_spaces,
    SUM(CASE WHEN execution_status = 'FAILED'   THEN 1 ELSE 0 END) AS failed_statements,
    SUM(CASE WHEN execution_status = 'CANCELED' THEN 1 ELSE 0 END) AS canceled_statements,
    ROUND(100.0 * SUM(CASE WHEN execution_status = 'FAILED' THEN 1 ELSE 0 END)
          / NULLIF(COUNT(*), 0), 2)                                AS failure_rate_pct,
    ROUND(AVG(total_duration_ms), 0)                               AS avg_duration_ms,
    ROUND(PERCENTILE(total_duration_ms, 0.95), 0)                  AS p95_duration_ms,
    ROUND(SUM(read_bytes) / (1024 * 1024 * 1024.0), 2)             AS total_read_gb,
    SUM(read_rows)                                                 AS total_rows_read,
    -- Health label drives the CI gate.
    CASE
        WHEN COUNT(*) = 0 THEN 'NO_DATA'
        WHEN 100.0 * SUM(CASE WHEN execution_status = 'FAILED' THEN 1 ELSE 0 END)
             / COUNT(*) > 5.0 THEN 'DEGRADED'
        WHEN PERCENTILE(total_duration_ms, 0.95) > 60000 THEN 'DEGRADED'
        ELSE 'HEALTHY'
    END                                                            AS health
FROM genie_queries;


-- ─── PART 2: Slowest statements — but hashed, not raw ──────────────────
-- The original's "top 50 by duration" listing, corrected:
--   * statement_text replaced with hash + length (non-PII, safe to share).
--   * Adds error_message (not redacted) for failure diagnosis.
--   * Includes the latency breakdown that tells you WHERE the time went.
--   * Hash lets you GROUP BY identical queries to spot repeated offenders
--     without ever reading the text.
WITH genie_queries AS (
    SELECT
        statement_id,
        executed_by,
        execution_status,
        statement_type,
        total_duration_ms,
        execution_duration_ms,
        waiting_for_compute_duration_ms,
        compilation_duration_ms,
        read_bytes,
        error_message,
        -- SHA-256 of statement_text. Raw text is <REDACTED> for non-PII
        -- users; hashing a redacted string yields a stable constant, which
        -- is fine — it still deduplicates repeated queries for those users.
        sha2(statement_text, 256)             AS statement_hash,
        LENGTH(statement_text)                AS statement_len
    FROM system.query.history
    WHERE start_time >= :audit_start
      AND start_time <  :audit_end
      AND (
            client_application = 'Databricks SQL Genie Space'
         OR query_source.genie_space_id IS NOT NULL
      )
)
SELECT
    statement_id,
    executed_by,
    execution_status,
    statement_type,
    statement_hash,
    statement_len,
    total_duration_ms,
    execution_duration_ms,
    waiting_for_compute_duration_ms,
    compilation_duration_ms,
    ROUND(read_bytes / (1024 * 1024 * 1024.0), 3) AS read_gb,
    error_message
FROM genie_queries
WHERE execution_status != 'FINISHED'    -- failures first: they explain cost
   OR total_duration_ms > 60000         -- or slow enough to matter
ORDER BY
    CASE WHEN execution_status != 'FINISHED' THEN 0 ELSE 1 END,
    total_duration_ms DESC
LIMIT 50;


-- ─── PART 3: Repeated-query offenders (the real audit value) ───────────
-- An audit that only lists rows cannot answer "is the same bad query run
-- every hour?" Grouping by statement_hash does, without PII access.
WITH genie_queries AS (
    SELECT
        sha2(statement_text, 256)     AS statement_hash,
        LENGTH(statement_text)        AS statement_len,
        statement_type,
        execution_status,
        total_duration_ms,
        read_bytes
    FROM system.query.history
    WHERE start_time >= :audit_start
      AND start_time <  :audit_end
      AND (
            client_application = 'Databricks SQL Genie Space'
         OR query_source.genie_space_id IS NOT NULL
      )
)
SELECT
    statement_hash,
    MIN(statement_len)                                      AS len_chars,
    COUNT(*)                                                AS run_count,
    COUNT(DISTINCT execution_status)                        AS distinct_statuses,
    SUM(CASE WHEN execution_status = 'FAILED' THEN 1 ELSE 0 END) AS failures,
    ROUND(AVG(total_duration_ms), 0)                        AS avg_ms,
    ROUND(SUM(read_bytes) / (1024 * 1024 * 1024.0), 2)     AS cumulative_read_gb
FROM genie_queries
GROUP BY statement_hash
HAVING COUNT(*) > 5                    -- ignore one-offs
ORDER BY cumulative_read_gb DESC, run_count DESC
LIMIT 20;
-- The top row is your highest-cost repeated query. If it is a failure
-- pattern, that is your first fix. If it is a full scan that could prune,
-- that is your first optimization. Nothing in the original surfaces this.


-- ─── PART 4: Raw statement text — admins only, opt-in ──────────────────
-- Uncomment only if the running identity is an account admin or a member
-- of databricks_pii_access. For everyone else, statement_text is
-- '<REDACTED>' and this returns no useful information.
--
-- SELECT
--     statement_id,
--     executed_by,
--     total_duration_ms,
--     statement_text
-- FROM system.query.history
-- WHERE start_time >= :audit_start
--   AND start_time <  :audit_end
--   AND (
--         client_application = 'Databricks SQL Genie Space'
--      OR query_source.genie_space_id IS NOT NULL
--   )
-- ORDER BY total_duration_ms DESC
-- LIMIT 50;
