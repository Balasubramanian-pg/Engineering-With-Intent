-- labs/06-curate-gold-views.sql
-- Creates dedicated AI schema with curated gold views.
CREATE SCHEMA IF NOT EXISTS retail_prod.gold_curated;

CREATE OR REPLACE VIEW retail_prod.gold_curated.daily_revenue_by_customer AS
SELECT 
    c.customer_name,
    c.region,
    o.event_date,
    SUM(o.net_amount) AS total_revenue_usd
FROM retail_prod.gold.orders o
JOIN retail_prod.gold.customers c ON o.customer_id = c.customer_id
GROUP BY ALL;
