#!/usr/bin/env bash
# labs/04-sql-formatter-check.sh
# Validates generated ANSI SQL against repository SQLFluff linting rules.

set -euo pipefail
SQL_FILE="${1:-./sample.sql}"
echo "Running SQLFluff lint against ${SQL_FILE} using Databricks dialect..."
sqlfluff lint "${SQL_FILE}" --dialect databricks --config .sqlfluff
echo "SQL formatting check passed cleanly."
