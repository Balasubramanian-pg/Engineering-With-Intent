-- labs/05-declare-constraints.sql
-- Declares non-enforced informational PK and FK constraints in Unity Catalog to guide Genie Code.

ALTER TABLE retail_prod.gold.customers 
ADD CONSTRAINT pk_customers PRIMARY KEY (customer_id) RELY;

ALTER TABLE retail_prod.gold.orders 
ADD CONSTRAINT fk_orders_customers 
FOREIGN KEY (customer_id) REFERENCES retail_prod.gold.customers(customer_id) RELY;

COMMENT ON COLUMN retail_prod.gold.orders.net_amount IS 
'Net recognized revenue in USD (synonyms: sales, turnover, topline)';
