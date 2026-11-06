-- labs/05-synonym-annotator.sql
-- Maps legacy ERP abbreviations to business synonyms in Unity Catalog comments.
COMMENT ON COLUMN retail_prod.gold.orders.vbeln IS 'Sales Document Number (synonyms: Order ID, Invoice, Sales Order)';
COMMENT ON COLUMN retail_prod.gold.orders.kunnr IS 'Customer Account Number (synonyms: Customer ID, Client Code)';
COMMENT ON COLUMN retail_prod.gold.orders.netwr IS 'Net Order Value (synonyms: Total Amount, Topline Revenue)';
