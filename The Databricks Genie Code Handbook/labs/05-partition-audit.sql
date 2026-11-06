-- labs/05-partition-audit.sql
-- Checks if queries on large tables apply partition pruning or trigger full scans.
EXPLAIN EXTENDED
SELECT * 
FROM retail_prod.gold.orders 
WHERE event_date = CURRENT_DATE() - 1;
