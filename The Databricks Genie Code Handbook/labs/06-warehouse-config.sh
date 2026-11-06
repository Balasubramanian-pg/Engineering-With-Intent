#!/usr/bin/env bash
# labs/06-warehouse-config.sh
# Applies 300s timeout and 5-minute auto-stop policies to Serverless SQL Warehouses.

set -euo pipefail
WAREHOUSE_ID="${1:-sample-warehouse-id}"

echo "Configuring guardrails on SQL Warehouse: ${WAREHOUSE_ID}..."
databricks sql-warehouses edit "${WAREHOUSE_ID}"   --auto-stop-mins 5   --max-num-clusters 2   --statement-timeout 300

echo "Warehouse guardrails updated: 300s statement timeout, 5 min auto-stop."
