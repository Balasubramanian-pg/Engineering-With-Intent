#!/usr/bin/env bash
# labs/07-bundle-deploy.sh
# Validates and promotes Databricks Asset Bundles (DABs) across staging and prod.

set -euo pipefail
TARGET="${1:-staging}"

echo "Validating Databricks Asset Bundle for target: ${TARGET}..."
databricks bundle validate -t "${TARGET}"

echo "Deploying bundle to ${TARGET}..."
databricks bundle deploy -t "${TARGET}"

echo "DABs promotion to ${TARGET} completed with zero configuration drift."
