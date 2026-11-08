#!/usr/bin/env bash
# labs/03-aitools-deploy.sh
# Declarative, idempotent installation of a workspace skill via Databricks CLI.
#
# Guarantees
# ----------
#   * Idempotent  — running N times yields the same target state. The script
#                   detects an existing install and either skips, updates, or
#                   replaces it depending on --mode. No duplication, no failure
#                   on re-run.
#   * Stateless   — writes only to TARGET_DIR (the declared target) and to a
#                   temp dir it cleans up via trap. Safe to run concurrently
#                   for *different* skills; for the same skill the last writer
#                   wins (documented, not silently raced).
#
# Exit codes
# ----------
#   0  success (installed, updated, or already up to date)
#   1  runtime failure (install or verify failed)
#   2  usage error
#   3  precondition failed (CLI missing, not authenticated, source invalid)
#
# NOTE: `databricks aitools ...` is assumed to exist as in the original.
# If your CLI uses `databricks workspace import-dir` or `databricks bundle
# deploy` instead, the two calls in `do_install` and `do_verify` are the only
# lines to change.

set -euo pipefail
IFS=$'\n\t'

# --- Defaults (overridable via flags) ---------------------------------------
SKILL_NAME="lakeflow-builder"
SKILL_PATH=""
TARGET_DIR=""
MODE="auto"        # auto | skip | update | force
DRY_RUN=0
VERBOSE=0

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
die()  { local code=$1; shift; err "$*"; exit "$code"; }
vlog() { (( VERBOSE )) && printf '  [v] %s\n' "$*" || true; }

# --- Usage ------------------------------------------------------------------
usage() {
    cat <<'EOF'
Usage: 03-aitools-deploy.sh [OPTIONS]

Install a workspace skill idempotently via the Databricks CLI.

Options:
  --skill NAME        Skill name (default: lakeflow-builder)
  --path PATH         Source skill directory (default: ./skills/<NAME>)
  --target DIR        Install target (default: /Workspace/.assistant/skills/<NAME>)
  --mode MODE         What to do if already installed:
                        auto    (default) skip if present, else install
                        skip    never overwrite; exit 0 if already installed
                        update  overwrite (re-install)
                        force   overwrite (re-install, alias of update)
  --dry-run           Print what would happen; do not call the CLI to install
  -v, --verbose       Extra diagnostic output
  -h, --help          Show this help

Exit codes: 0 success · 1 failure · 2 usage · 3 precondition
EOF
}

# --- Argument parsing -------------------------------------------------------
while (( $# )); do
    case "$1" in
        --skill)   SKILL_NAME="${2:?--skill needs a value}"; shift 2 ;;
        --path)    SKILL_PATH="${2:?--path needs a value}";  shift 2 ;;
        --target)  TARGET_DIR="${2:?--target needs a value}"; shift 2 ;;
        --mode)    MODE="${2:?--mode needs a value}";        shift 2 ;;
        --dry-run) DRY_RUN=1; shift ;;
        -v|--verbose) VERBOSE=1; shift ;;
        -h|--help) usage; exit 0 ;;
        --)        shift; break ;;
        -*)        usage >&2; die 2 "unknown option: $1" ;;
        *)         usage >&2; die 2 "unexpected argument: $1" ;;
    esac
done

# Resolve defaults now that flags are parsed.
SKILL_PATH="${SKILL_PATH:-./skills/${SKILL_NAME}}"
TARGET_DIR="${TARGET_DIR:-/Workspace/.assistant/skills/${SKILL_NAME}}"

case "$MODE" in
    auto|skip|update|force) ;;
    *) die 2 "invalid --mode '$MODE' (expected auto|skip|update|force)" ;;
esac

# --- Temp dir + cleanup (stateless) -----------------------------------------
TMPDIR_RUN=""
cleanup() {
    [[ -n "$TMPDIR_RUN" && -d "$TMPDIR_RUN" ]] && rm -rf "$TMPDIR_RUN"
}
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

    local ver
    ver=$(databricks --version 2>/dev/null || true)
    [[ -n "$ver" ]] && vlog "cli: $ver"

    # Authentication — prefer `current-user me`; treat failure as unauthenticated.
    if ! databricks current-user me >/dev/null 2>&1; then
        # Some environments use a different auth subcommand; fall back to
        # checking that SOME identity is available before failing hard.
        if ! databricks auth describe >/dev/null 2>&1; then
            die 3 "databricks CLI is not authenticated (run: databricks auth login)"
        fi
    fi
    vlog "auth: ok"
}

# --- Source validation (the "Validating..." the original faked) -------------
validate_source() {
    log "Validating skill source at ${SKILL_PATH}..."

    [[ -d "$SKILL_PATH" ]] || die 3 "skill directory not found: ${SKILL_PATH}"

    local manifest="${SKILL_PATH}/SKILL.md"
    [[ -f "$manifest" ]] || die 3 "SKILL.md missing in ${SKILL_PATH}"

    # Frontmatter must be present and closed. This is a structural check,
    # not a YAML parse — catches the common "forgot the closing ---" bug.
    head -n 1 "$manifest" | grep -qx -- '---' \
        || die 3 "SKILL.md frontmatter must begin with a '---' line on line 1"

    local fm_end
    fm_end=$(awk 'NR>1 && /^---$/{print NR; exit}' "$manifest")
    [[ -n "$fm_end" && "$fm_end" -gt 2 ]] \
        || die 3 "SKILL.md frontmatter is empty or not closed with '---'"

    # Require a `name:` key matching the declared skill (frontmatter lie is
    # the second most common bug after a missing file).
    local declared_name
    declared_name=$(awk -v end="$fm_end" '
        NR==1 { next } NR>=end { exit } /^name:[[:space:]]*/ { sub(/^name:[[:space:]]*/,""); print; exit }
    ' "$manifest")
    [[ -n "$declared_name" ]] || die 3 "SKILL.md frontmatter has no 'name:' field"
    [[ "$declared_name" == "$SKILL_NAME" ]] \
        || die 3 "SKILL.md name '$declared_name' does not match --skill '$SKILL_NAME'"

    # Reject empty skill dirs (no scripts/templates) — install would succeed
    # but produce a useless skill.
    local file_count
    file_count=$(find "$SKILL_PATH" -type f ! -name 'SKILL.md' | wc -l | tr -d ' ')
    (( file_count > 0 )) || warn "skill has no files besides SKILL.md"

    vlog "manifest: ${manifest} (${file_count} extra file(s), name=${declared_name})"
}

# --- Idempotency: detect existing install -----------------------------------
is_installed() {
    # Use the CLI's own view of the workspace so this works whether or not
    # the target path is locally mounted.
    databricks aitools list 2>/dev/null \
        | awk -v name="$SKILL_NAME" '$0 ~ name { found=1 } END { exit !found }'
}

# --- Install ----------------------------------------------------------------
do_install() {
    local will_install=0

    if is_installed; then
        case "$MODE" in
            auto)   log "Already installed at ${TARGET_DIR}; skipping (mode=auto)."
                    return 0 ;;
            skip)   log "Already installed; skipping (mode=skip)."; return 0 ;;
            update|force)
                    log "Already installed; re-installing (mode=${MODE})."
                    will_install=1 ;;
        esac
    else
        will_install=1
    fi

    if (( DRY_RUN )); then
        (( will_install )) && info "[dry-run] would install ${SKILL_NAME} from ${SKILL_PATH} -> ${TARGET_DIR}"
        return 0
    fi

    log "Installing ${SKILL_NAME} -> ${TARGET_DIR}..."
    # --overwrite is assumed to exist for idempotent updates; drop it if your
    # CLI version doesn't support it. Some CLIs accept --force instead.
    if [[ "$MODE" == "update" || "$MODE" == "force" ]]; then
        databricks aitools install \
            --path "$SKILL_PATH" --target-dir "$TARGET_DIR" --overwrite
    else
        databricks aitools install \
            --path "$SKILL_PATH" --target-dir "$TARGET_DIR"
    fi
}

# --- Verify (real verification, not just "list ran") ------------------------
do_verify() {
    if (( DRY_RUN )); then
        info "[dry-run] would verify '${SKILL_NAME}' appears in 'aitools list'"
        return 0
    fi

    log "Verifying installed skill..."
    local listing
    if ! listing=$(databricks aitools list 2>&1); then
        die 1 "verification failed: 'databricks aitools list' returned non-zero"
    fi
    vlog "list output: ${listing}"

    if ! grep -Fq -- "$SKILL_NAME" <<<"$listing"; then
        err "verification failed: '${SKILL_NAME}' not present in 'aitools list'"
        err "listing was:"
        printf '%s\n' "$listing" | sed 's/^/    /' >&2
        exit 1
    fi
    info "verified: ${SKILL_NAME} present in workspace listing."
}

# --- Main -------------------------------------------------------------------
preflight
validate_source
do_install
do_verify

log "Skill deployment completed successfully."
