-- labs/06-audit-query.sql
-- Audits Databricks Genie Code queries in system.query.history.
SELECT 
    statement_id,
    executed_by,
    start_time,
    execution_status,
    total_duration_ms / 1000.0 AS duration_seconds,
    read_bytes / (1024 * 1024 * 1024.0) AS read_gb,
    statement_text
FROM system.query.history
WHERE client_application = 'Genie'
  AND start_time >= CURRENT_TIMESTAMP() - INTERVAL 30 DAYS
ORDER BY total_duration_ms DESC
LIMIT 50;
