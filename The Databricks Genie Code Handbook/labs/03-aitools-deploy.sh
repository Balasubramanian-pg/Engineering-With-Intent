#!/usr/bin/env bash
# labs/03-aitools-deploy.sh
# Declarative installation of workspace skills using the Databricks CLI.

set -euo pipefail

SKILL_NAME="lakeflow-builder"
SKILL_PATH="./skills/${SKILL_NAME}"
TARGET_DIR="/Workspace/.assistant/skills/${SKILL_NAME}"

echo "Validating skill frontmatter and scripts in ${SKILL_PATH}..."
echo "Installing ${SKILL_NAME} to ${TARGET_DIR} via databricks CLI..."
databricks aitools install --path "${SKILL_PATH}" --target-dir "${TARGET_DIR}"

echo "Verifying installed skills..."
databricks aitools list
echo "Skill deployment completed successfully."
