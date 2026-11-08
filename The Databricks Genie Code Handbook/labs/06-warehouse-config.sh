#!/usr/bin/env bash
# labs/06-warehouse-config.sh
# Apply guardrails to Databricks SQL Warehouses: auto-stop, max clusters,
# and statement timeout. Idempotent: re-running with the same values is a
# no-op. Stateless: no temp files, no session state, no globals.
#
# Guarantees
# ----------
#   * Idempotent — fetches current config first; only calls PATCH if a
#                  field differs. Re-running is always safe.
#   * Stateless  — writes only to the warehouse via the API; reads only
#                  current config; no files left behind.
#
# Exit codes
# ----------
#   0  success (applied or already up to date)
#   1  runtime failure (API error, validation)
#   2  usage error
#   3  precondition failed (CLI missing, not authenticated, warehouse absent)
#
# Prerequisites
# -------------
#   * Databricks CLI >= 0.205, authenticated (`databricks auth login`).
#   * For statement_timeout: workspace admin must enable the
#     "Warehouse Statement Timeout" preview (Beta). Without it, the field
#     is silently ignored by the API — this script checks and warns.

set -euo pipefail
IFS=$'\n\t'

# --- Defaults (overridable via flags) ---------------------------------------
WAREHOUSE_ID=""
AUTO_STOP_MINS=5
MAX_NUM_CLUSTERS=2
STATEMENT_TIMEOUT=300
DRY_RUN=0
FORCE=0
VERBOSE=0
TMP_DIR=""

# --- Output helpers ---------------------------------------------------------
if [[ -t 1 ]] && command -v tput >/dev/null 2>&1 && [[ -n "${TERM:-}" ]]; then
    C_RED=$(tput setaf 1); C_YEL=$(tput setaf 3); C_GRN=$(tput setaf 2); C_RST=$(tput sgr0)
else
    C_RED=""; C_YEL=""; C_GRN=""; C_RST=""
fi
log()  { printf '%s\n' "$*"; }
info() { printf '  %s\n' "$*"; }
warn() { printf '%sWARN:%s %s\n' "$C_YEL" "$C_RST" "$*" >&2; }
err()  { printf '%sERROR:%s %s\n' "$C_RED" "$C_RST" "$*" >&2; }
ok()   { printf '%s%s%s\n' "$C_GRN" "$*" "$C_RST"; }
die()  { local code=$1; shift; err "$*"; exit "$code"; }
vlog() { (( VERBOSE )) && printf '  [v] %s\n' "$*" || true; }

# --- Usage ------------------------------------------------------------------
usage() {
    cat <<'EOF'
Usage: 06-warehouse-config.sh [OPTIONS] WAREHOUSE_ID

Apply auto-stop, max-cluster, and statement-timeout guardrails to a SQL
warehouse.

Options:
  --auto-stop-mins N       Idle minutes before auto-stop (default: 5)
  --max-num-clusters N     Max clusters for autoscaling (default: 2)
  --statement-timeout N    Statement timeout in seconds (default: 300;
                           range 0–172800; 0 = system default)
  --dry-run                Print the payload and diff; do not apply
  --force                  Apply even if current config matches
  -v, --verbose            Extra diagnostics
  -h, --help               Show this help

Exit codes: 0 success · 1 runtime failure · 2 usage · 3 precondition
EOF
}

# --- Argument parsing -------------------------------------------------------
while (( $# )); do
    case "$1" in
        -h|--help)          usage; exit 0 ;;
        --auto-stop-mins)   AUTO_STOP_MINS="${2:?}"; shift 2 ;;
        --max-num-clusters) MAX_NUM_CLUSTERS="${2:?}"; shift 2 ;;
        --statement-timeout) STATEMENT_TIMEOUT="${2:?}"; shift 2 ;;
        --dry-run)          DRY_RUN=1; shift ;;
        --force)            FORCE=1; shift ;;
        -v|--verbose)       VERBOSE=1; shift ;;
        --)                 shift; break ;;
        -*)                 usage >&2; die 2 "unknown option: $1" ;;
        *)                  [[ -z "$WAREHOUSE_ID" ]] && WAREHOUSE_ID="$1" || { usage >&2; die 2 "unexpected argument: $1"; }; shift ;;
    esac
done

[[ -n "$WAREHOUSE_ID" ]] || { usage >&2; die 2 "WAREHOUSE_ID is required"; }

# --- Validation -------------------------------------------------------------
[[ "$AUTO_STOP_MINS" =~ ^[0-9]+$ ]] && (( AUTO_STOP_MINS >= 0 )) \
    || die 2 "--auto-stop-mins must be a non-negative integer"
[[ "$MAX_NUM_CLUSTERS" =~ ^[0-9]+$ ]] && (( MAX_NUM_CLUSTERS >= 1 )) \
    || die 2 "--max-num-clusters must be a positive integer"
[[ "$STATEMENT_TIMEOUT" =~ ^[0-9]+$ ]] && (( STATEMENT_TIMEOUT >= 0 && STATEMENT_TIMEOUT <= 172800 )) \
    || die 2 "--statement-timeout must be an integer in [0, 172800] (2 days)"

# --- Temp dir + cleanup -----------------------------------------------------
cleanup() { [[ -n "$TMP_DIR" && -d "$TMP_DIR" ]] && rm -rf "$TMP_DIR"; }
trap cleanup EXIT INT TERM

on_err() {
    local rc=$?
    err "failed at line $LINENO (rc=$rc): ${BASH_COMMAND}"
    exit 1
}
trap on_err ERR

# --- Preflight --------------------------------------------------------------
preflight() {
    log "Preflight: checking environment..."
    command -v databricks >/dev/null 2>&1 \
        || die 3 "databricks CLI not found on PATH"

    if ! databricks auth describe >/dev/null 2>&1; then
        die 3 "databricks CLI is not authenticated (run: databricks auth login)"
    fi

    # Confirm the warehouse exists before PATCHing it.
    if ! databricks api get "/api/2.0/sql/warehouses/${WAREHOUSE_ID}" >/dev/null 2>&1; then
        die 3 "warehouse not found or not accessible: ${WAREHOUSE_ID}"
    fi
    info "preflight ok (warehouse exists, CLI authenticated)"
}

# --- Idempotency: fetch current config, decide if change is needed ----------
needs_update() {
    local current_json
    current_json=$(databricks api get "/api/2.0/sql/warehouses/${WAREHOUSE_ID}")

    local cur_auto cur_max cur_timeout
    cur_auto=$(jq -r '.auto_stop_mins // empty' <<<"$current_json")
    cur_max=$(jq -r '.max_num_clusters // empty' <<<"$current_json")
    cur_timeout=$(jq -r '.statement_timeout // empty' <<<"$current_json")

    vlog "current: auto_stop_mins=${cur_auto} max_num_clusters=${cur_max} statement_timeout=${cur_timeout}"

    if [[ "$cur_auto" == "$AUTO_STOP_MINS" ]] \
       && [[ "$cur_max" == "$MAX_NUM_CLUSTERS" ]] \
       && [[ "$cur_timeout" == "$STATEMENT_TIMEOUT" ]]; then
        return 1   # already configured
    fi
    return 0
}

# --- Apply ------------------------------------------------------------------
apply_config() {
    local payload
    payload=$(cat <<JSON
{
  "auto_stop_mins": ${AUTO_STOP_MINS},
  "max_num_clusters": ${MAX_NUM_CLUSTERS},
  "statement_timeout": ${STATEMENT_TIMEOUT}
}
JSON
)

    if (( DRY_RUN )); then
        info "[dry-run] would PATCH /api/2.0/sql/warehouses/${WAREHOUSE_ID}"
        printf '%s\n' "$payload" | sed 's/^/    /'
        return 0
    fi

    info "Applying: auto_stop_mins=${AUTO_STOP_MINS}, max_num_clusters=${MAX_NUM_CLUSTERS}, statement_timeout=${STATEMENT_TIMEOUT}s"
    # The Update Warehouse API uses PATCH for partial updates. Only the
    # fields in the payload are modified; all others retain current values.
    databricks api patch "/api/2.0/sql/warehouses/${WAREHOUSE_ID}" \
        --json "$payload" >/dev/null
    ok "applied."
}

# --- Verify -----------------------------------------------------------------
verify_config() {
    local after_json
    after_json=$(databricks api get "/api/2.0/sql/warehouses/${WAREHOUSE_ID}")

    local auto max timeout
    auto=$(jq -r '.auto_stop_mins' <<<"$after_json")
    max=$(jq -r '.max_num_clusters' <<<"$after_json")
    timeout=$(jq -r '.statement_timeout // 0' <<<"$after_json")

    if [[ "$auto" == "$AUTO_STOP_MINS" ]] \
       && [[ "$max" == "$MAX_NUM_CLUSTERS" ]] \
       && [[ "$timeout" == "$STATEMENT_TIMEOUT" ]]; then
        ok "verified: auto_stop_mins=${auto}, max_num_clusters=${max}, statement_timeout=${timeout}s"
        return 0
    fi

    err "verification failed: expected auto=${AUTO_STOP_MINS} max=${MAX_NUM_CLUSTERS} timeout=${STATEMENT_TIMEOUT}, got auto=${auto} max=${max} timeout=${timeout}"
    # Special case: statement_timeout is Beta. If the field is absent (null)
    # or 0, the workspace admin has not enabled the preview.
    if [[ "$timeout" == "0" || "$timeout" == "null" ]] && (( STATEMENT_TIMEOUT > 0 )); then
        warn "statement_timeout was not applied. This is a Beta feature —"
        warn "a workspace admin must enable the 'Warehouse Statement Timeout' preview."
    fi
    return 1
}

# --- Main ------------------------------------------------------------------
preflight

log "Configuring guardrails on SQL Warehouse: ${WAREHOUSE_ID}"

if (( FORCE )); then
    info "force mode: applying regardless of current state"
    apply_config
elif ! needs_update; then
    info "already configured (auto_stop_mins=${AUTO_STOP_MINS}, max_num_clusters=${MAX_NUM_CLUSTERS}, statement_timeout=${STATEMENT_TIMEOUT}s); nothing to do"
    ok "no changes needed."
    exit 0
else
    apply_config
fi

if (( ! DRY_RUN )); then
    verify_config || exit 1
fi

log "Warehouse guardrails configured: ${AUTO_STOP_MINS} min auto-stop, ${MAX_NUM_CLUSTERS} max clusters, ${STATEMENT_TIMEOUT}s statement timeout."
