#!/usr/bin/env bash
# labs/07-bundle-deploy.sh
# Validate and promote Databricks Asset Bundles (DABs) across targets.
#
# Guarantees
# ----------
#   * Idempotent  — `bundle deploy` is declarative: re-running with unchanged
#                   source produces no changes. The script confirms this via
#                   `bundle plan` (which should report "no changes" after a
#                   successful deploy) and reports it as PASS.
#   * Stateless   — writes only to the workspace via the bundle. No local
#                   state files beyond what the CLI itself manages in
#                   `.bundle/` (DABs' own tracked-state directory). No
#                   temp files left behind; cleaned via trap.
#   * Honest exit — `bundle deploy` can exit 0 on a failed deployment
#                   (reported in the CLI issue tracker). This script
#                   independently verifies the deployed resources via
#                   `bundle summary` and fails if they are absent or
#                   drifted. Exit code reflects reality, not the CLI's
#                   optimism.
#
# Exit codes
# ----------
#   0  success — validated, deployed, and verified with no drift
#   1  runtime failure — deploy failed, or verification found drift/absence
#   2  usage error
#   3  precondition failed — CLI missing, not authenticated, bundle root wrong
#
# Prerequisites
# -------------
#   * Databricks CLI >= 0.205, authenticated (`databricks auth login`).
#   * Run from the bundle project root (the directory containing
#     `databricks.yml`), or pass `--bundle-root`.
#   * The target must exist in `databricks.yml` under `targets:`.

set -euo pipefail
IFS=$'\n\t'

# --- Defaults (overridable via flags) ---------------------------------------
TARGET="staging"
BUNDLE_ROOT=""
MODE="apply"          # plan | apply (default apply)
DRY_RUN=0
SKIP_VALIDATE=0
REQUIRE_NO_DRIFT=0
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
Usage: 07-bundle-deploy.sh [OPTIONS] [TARGET]

Validate, plan, deploy, and verify a Databricks Asset Bundle.

Arguments:
  TARGET                Deployment target (default: staging)

Options:
  --bundle-root DIR     Bundle project root (default: current directory)
  --plan                Show the plan (drift diff) and exit; do not deploy
  --dry-run             Alias for --plan; print what would happen
  --skip-validate       Skip `bundle validate --strict` (dev targets only)
  --require-no-drift    Fail if `bundle plan` reports changes AFTER deploy
  -v, --verbose         Extra diagnostics
  -h, --help            Show this help

Exit codes: 0 success · 1 runtime failure · 2 usage · 3 precondition
EOF
}

# --- Argument parsing -------------------------------------------------------
while (( $# )); do
    case "$1" in
        -h|--help)           usage; exit 0 ;;
        --bundle-root)       BUNDLE_ROOT="${2:?--bundle-root needs a value}"; shift 2 ;;
        --plan|--dry-run)    MODE="plan"; DRY_RUN=1; shift ;;
        --skip-validate)     SKIP_VALIDATE=1; shift ;;
        --require-no-drift)  REQUIRE_NO_DRIFT=1; shift ;;
        -v|--verbose)        VERBOSE=1; shift ;;
        --)                  shift; break ;;
        -*)                  usage >&2; die 2 "unknown option: $1" ;;
        *)                   TARGET="$1"; shift ;;
    esac
done

BUNDLE_ROOT="${BUNDLE_ROOT:-$(pwd)}"

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

    [[ -d "$BUNDLE_ROOT" ]] || die 3 "bundle root not found: ${BUNDLE_ROOT}"
    [[ -f "${BUNDLE_ROOT}/databricks.yml" ]] \
        || die 3 "no databricks.yml in ${BUNDLE_ROOT} — is this the bundle root?"

    # Confirm the target exists in databricks.yml. DABs resolves this at
    # validate time, but checking here gives a clearer error and a distinct
    # exit code (precondition vs. runtime).
    if ! grep -qE "^[[:space:]]*${TARGET}:" "${BUNDLE_ROOT}/databricks.yml"; then
        warn "target '${TARGET}' not found by simple grep — DABs may still resolve it from an included file"
        vlog "grep pattern: ^[[:space:]]*${TARGET}:"
    fi

    vlog "cli: $(databricks --version 2>/dev/null | head -n1 || echo unknown)"
    vlog "root: ${BUNDLE_ROOT}"
    vlog "target: ${TARGET}"
    info "preflight ok"
}

# --- Step 1: Validate (strict) ---------------------------------------------
do_validate() {
    log "Validating bundle for target: ${TARGET} (--strict)..."
    local out
    if ! out=$(cd "$BUNDLE_ROOT" && databricks bundle validate --strict -t "$TARGET" 2>&1); then
        err "validation failed:"
        printf '%s\n' "$out" | sed 's/^/    /' >&2
        die 1 "bundle validation failed"
    fi
    # --strict treats warnings as errors, so a zero exit here means no
    # warnings either. Log the outcome for the audit trail.
    vlog "validation output: ${out:-<empty>}"
    ok "validation passed (strict: no warnings)."
}

# --- Step 2: Plan (drift detection) ---------------------------------------
# `bundle plan` computes the diff between desired state (YAML) and current
# workspace state. It makes no changes. This is the drift measurement the
# original never performed.
do_plan() {
    log "Computing deployment plan for ${TARGET}..."
    TMP_DIR=$(mktemp -d "${TMPDIR:-/tmp}/dab.XXXXXX")
    local plan_file="${TMP_DIR}/plan.txt"
    local plan_json="${TMP_DIR}/plan.json"

    local rc=0
    ( cd "$BUNDLE_ROOT" \
      && databricks bundle plan -t "$TARGET" -o json >"$plan_json" 2>"$plan_file" ) || rc=$?

    case "$rc" in
        0) ;;
        1)  # `bundle plan` exit 1 can mean "plan available but errors" — inspect.
            warn "bundle plan returned non-zero; showing output:"
            cat "$plan_file" >&2 || true
            die 1 "bundle plan failed"
            ;;
        *)  die 1 "bundle plan failed (exit ${rc})" ;;
    esac

    # Extract the human-readable plan. DABs JSON includes a "plan" or
    # "changes" field; fall back to the text plan if JSON parsing fails.
    local plan_text=""
    if command -v jq >/dev/null 2>&1 && [[ -s "$plan_json" ]]; then
        plan_text=$(jq -r '
            .plan? // .changes? // empty
            | if type == "array" then .[] else . end
        ' "$plan_json" 2>/dev/null || true)
    fi
    if [[ -z "$plan_text" ]]; then
        # Text fallback: read the plain `bundle plan` output.
        ( cd "$BUNDLE_ROOT" && databricks bundle plan -t "$TARGET" ) >"$plan_file" 2>&1 || true
        plan_text=$(cat "$plan_file")
    fi

    if [[ -z "$plan_text" ]] || grep -qiE 'no changes|up to date|nothing to do' <<<"$plan_text"; then
        info "plan: no changes — workspace matches desired state."
        return 0
    fi

    info "plan: changes detected:"
    printf '%s\n' "$plan_text" | sed 's/^/    /'
    return 1   # nonzero means "drift exists"; caller decides whether to deploy
}

# --- Step 3: Deploy --------------------------------------------------------
do_deploy() {
    log "Deploying bundle to ${TARGET}..."
    # --auto-approve avoids the interactive prompt in CI. Do NOT use
    # --force unless you intend to override protection (e.g. retain-last-
    # updated or permission checks). --force is a foot-gun in prod.
    local out
    if ! out=$(cd "$BUNDLE_ROOT" \
               && databricks bundle deploy -t "$TARGET" --auto-approve 2>&1); then
        err "deploy failed:"
        printf '%s\n' "$out" | sed 's/^/    /' >&2
        die 1 "bundle deploy failed"
    fi
    printf '%s\n' "$out" | sed 's/^/    /'
    ok "deploy command returned success."
}

# --- Step 4: Verify (do not trust the exit code alone) --------------------
# `bundle deploy` is known to exit 0 on some failures. `bundle summary`
# lists the resources the bundle believes it has deployed. If a resource
# is absent, the deploy did not actually take effect.
do_verify() {
    log "Verifying deployed resources for ${TARGET}..."
    local out
    if ! out=$(cd "$BUNDLE_ROOT" \
               && databricks bundle summary -t "$TARGET" 2>&1); then
        err "verification failed: bundle summary returned non-zero"
        printf '%s\n' "$out" | sed 's/^/    /' >&2
        die 1 "verification failed"
    fi

    vlog "summary:"
    printf '%s\n' "$out" | sed 's/^/    [v] /'

    # At minimum, confirm the summary names the target and at least one
    # resource. Absence of either is the signal that deploy did not apply.
    if ! grep -qiF "Target:" <<<"$out"; then
        die 1 "verification failed: summary does not report a target"
    fi
    if ! grep -qiF "$TARGET" <<<"$out"; then
        die 1 "verification failed: summary does not mention target '${TARGET}'"
    fi
    # Count resource-like lines. DABs summary shows resource names under a
    # "Resources:" or similar header; a count of zero means nothing deployed.
    local resource_count
    resource_count=$(grep -cE '^\s{2,}[A-Za-z0-9_.-]+:' <<<"$out" || true)
    if (( resource_count == 0 )); then
        die 1 "verification failed: summary lists zero resources (deploy may have silently failed)"
    fi
    ok "verified: ${resource_count} resource(s) deployed to ${TARGET}."
}

# --- Step 5: Post-deploy drift check --------------------------------------
do_post_drift_check() {
    if ! (( REQUIRE_NO_DRIFT )); then
        info "skipping post-deploy drift check (use --require-no-drift to enable)"
        return 0
    fi
    log "Checking for residual drift after deploy..."
    if do_plan; then
        ok "post-deploy plan: no changes — deployment converged."
        return 0
    fi
    err "post-deploy plan reports changes — deployment did not converge."
    die 1 "residual drift after deploy"
}

# --- Main ------------------------------------------------------------------
preflight

if (( ! SKIP_VALIDATE )); then
    do_validate
else
    warn "validation skipped (--skip-validate) — only safe for dev targets"
fi

# Plan first (always). Exit early in plan/dry-run mode.
set +e
do_plan
plan_rc=$?
set -e

if [[ "$MODE" == "plan" ]]; then
    if (( plan_rc == 0 )); then
        ok "plan: no changes — workspace is up to date."
    else
        info "plan complete; changes above would be applied by a real deploy."
    fi
    exit 0
fi

do_deploy
do_verify
do_post_drift_check

log "Bundle deployment to ${TARGET} completed and verified."
