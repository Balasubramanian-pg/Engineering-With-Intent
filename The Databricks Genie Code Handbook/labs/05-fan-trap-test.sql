-- labs/05-fan-trap-test.sql
-- Demonstrates fan trap metric duplication vs CTE pre-aggregation.
WITH pre_aggregated_promotions AS (
    SELECT order_id, COUNT(promotion_id) AS promo_cnt
    FROM retail_prod.gold.order_promotions
    GROUP BY order_id
)
SELECT 
    o.order_id,
    o.net_amount,
    COALESCE(p.promo_cnt, 0) AS applied_promotions
FROM retail_prod.gold.orders o
LEFT JOIN pre_aggregated_promotions p ON o.order_id = p.order_id;
