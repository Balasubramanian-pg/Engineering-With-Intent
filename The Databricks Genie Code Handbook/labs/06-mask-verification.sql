-- labs/06-mask-verification.sql
-- Verifies dynamic column masks and row filters for Genie Code executions.
SELECT 
    customer_id,
    tax_identifier, -- Masked for non-compliance personas
    region,
    total_spend
FROM retail_prod.gold.customers;
