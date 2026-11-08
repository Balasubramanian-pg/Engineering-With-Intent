#!/usr/bin/env bash
# labs/04-sql-formatter-check.sh
# Validate SQL against repo SQLFluff rules. Default dialect: databricks.
#
# Guarantees
# ----------
#   * Idempotent (default) — lint mode is READ-ONLY: the SQL file is never
#     modified. Re-running yields identical output and exit code.
#   * Idempotent (--fix)   — fix mode mutates the file, but is *state*-
#     idempotent: running twice leaves the file in the same formatted
#     state, and the second run reports "no changes needed."
#   * Stateless            — no globals, no cache dirs, no temp files left
#     behind (cleaned via trap). SQLFluff's own internal cache is not
#     consulted to make decisions and does not affect output.
#   * Atomic (--fix)       — writes to a temp file, then `mv` into place
#     only on success. A crash mid-fix cannot leave a half-formatted file.
#
# Exit codes
# ----------
#   0  clean — no lint violations
#   1  lint violations found (CI should block on this)
#   2  SQLFluff fatal error (bad config, parse failure — NOT a lint issue)
#   3  precondition failed (tool, file, or config missing)
#   4  usage error (bad flag, extra argument)
#
# Distinguishing 1 vs 2 matters: lint violations mean "fix the SQL", fatal
# errors mean "fix the tooling/config". A pipeline that blocks on 1 but
# pages on 2 needs them separable.

set -euo pipefail
IFS=$'\n\t'

# --- Defaults (overridable via flags) ---------------------------------------
SQL_FILE=""
CONFIG=".sqlfluff"
DIALECT="databricks"
FORMAT="human"          # sqlfluff's --format: human|json|yaml|github-annotation...
FIX=0                   # opt-in mutation
DRY_RUN=0               # with --fix, show diff without replacing
QUIET=0
VERBOSE=0
TMP_DIR=""              # set by --fix; cleaned via trap

# --- Output helpers ---------------------------------------------------------
if [[ -t 1 ]] && command -v tput >/dev/null 2>&1 && [[ -n "${TERM:-}" ]]; then
    C_RED=$(tput setaf 1); C_YEL=$(tput setaf 3); C_GRN=$(tput setaf 2); C_RST=$(tput sgr0)
else
    C_RED=""; C_YEL=""; C_GRN=""; C_RST=""
fi
info()    { (( QUIET )) && return 0; printf '  %s\n' "$*"; }
log()     { (( QUIET )) && return 0; printf '%s\n' "$*"; }
warn()    { printf '%sWARN:%s %s\n'  "$C_YEL" "$C_RST" "$*" >&2; }
err()     { printf '%sERROR:%s %s\n' "$C_RED" "$C_RST" "$*" >&2; }
ok()      { printf '%s%s%s\n' "$C_GRN" "$*" "$C_RST"; }
die()     { local code=$1; shift; err "$*"; exit "$code"; }
vlog()    { (( VERBOSE )) && printf '  [v] %s\n' "$*" || true; }

# --- Usage ------------------------------------------------------------------
usage() {
    cat <<'EOF'
Usage: 04-sql-formatter-check.sh [OPTIONS] [SQL_FILE]

Lint (or fix) a SQL file against repo SQLFluff rules.

Arguments:
  SQL_FILE            Path to SQL file (default: ./sample.sql)

Options:
  --dialect DIALECT   SQLFluff dialect (default: databricks)
  --config PATH       SQLFluff config path (default: .sqlfluff)
  --format FMT        Output format: human|json|yaml|github-annotation|...
  --fix               Rewrite the file in-place (opt-in; uses temp + mv)
  --dry-run           With --fix: print a diff, do not replace the file
  -q, --quiet         Only print errors
  -v, --verbose       Extra diagnostics
  -h, --help          Show this help

Exit codes: 0 clean · 1 lint violations · 2 SQLFluff fatal error ·
            3 precondition · 4 usage
EOF
}

# --- Argument parsing -------------------------------------------------------
while (( $# )); do
    case "$1" in
        -h|--help)     usage; exit 0 ;;
        --fix)         FIX=1; shift ;;
        --dry-run)     DRY_RUN=1; shift ;;
        --dialect)     DIALECT="${2:?--dialect needs a value}"; shift 2 ;;
        --config)      CONFIG="${2:?--config needs a value}";   shift 2 ;;
        --format)      FORMAT="${2:?--format needs a value}";   shift 2 ;;
        -q|--quiet)    QUIET=1; shift ;;
        -v|--verbose)  VERBOSE=1; shift ;;
        --)            shift; break ;;
        -*)            usage >&2; die 4 "unknown option: $1" ;;
        *)
            if [[ -z "$SQL_FILE" ]]; then
                SQL_FILE="$1"; shift
            else
                usage >&2; die 4 "unexpected argument: $1"
            fi
            ;;
    esac
done
SQL_FILE="${SQL_FILE:-./sample.sql}"

# --dry-run without --fix is nonsensical (lint doesn't modify anything).
if (( DRY_RUN && ! FIX )); then
    die 4 "--dry-run only makes sense with --fix"
fi

# --- Temp dir + cleanup (stateless) ----------------------------------------
cleanup() {
    if [[ -n "$TMP_DIR" && -d "$TMP_DIR" ]]; then
        rm -rf "$TMP_DIR"
    fi
}
trap cleanup EXIT INT TERM

on_err() {
    local rc=$?
    err "failed at line $LINENO (rc=$rc): ${BASH_COMMAND}"
    exit 2   # unexpected internal failure — not a lint issue
}
trap on_err ERR

# --- Preflight --------------------------------------------------------------
preflight() {
    log "Preflight: checking environment..."

    command -v sqlfluff >/dev/null 2>&1 \
        || die 3 "sqlfluff not found on PATH (pip install sqlfluff)"

    local ver
    ver=$(sqlfluff --version 2>/dev/null | head -n1 || true)
    vlog "sqlfluff: ${ver:-unknown version}"

    if [[ ! -f "$SQL_FILE" ]]; then
        die 3 "SQL file not found: ${SQL_FILE}"
    fi
    if [[ ! -r "$SQL_FILE" ]]; then
        die 3 "SQL file not readable: ${SQL_FILE}"
    fi

    # Resolve config to an absolute path BEFORE any `cd`/tmp work, so the
    # config is looked up relative to the invocation directory, not the
    # temp directory we may create later. This is a real trap with
    # --config + --fix: the original script would silently lint without
    # the config when invoked from a different directory.
    if [[ -e "$CONFIG" ]]; then
        CONFIG=$(cd "$(dirname "$CONFIG")" && pwd)/$(basename "$CONFIG")
        vlog "config resolved: ${CONFIG}"
        if [[ ! -f "$CONFIG" ]]; then
            die 3 "config path is not a file: ${CONFIG}"
        fi
    else
        die 3 "SQLFluff config not found: ${CONFIG} (use --config to point at one)"
    fi

    vlog "file: ${SQL_FILE} (${#SQL_FILE} chars in path)"
    vlog "dialect: ${DIALECT}  format: ${FORMAT}"
    info "preflight ok"
}

# --- Lint ------------------------------------------------------------------
do_lint() {
    log "Linting ${SQL_FILE} (dialect=${DIALECT}, format=${FORMAT})..."
    local rc=0
    # shellcheck disable=SC2086  # intentional: FORMAT is not user-quoted-by-accident
    sqlfluff lint "$SQL_FILE" \
        --dialect "$DIALECT" \
        --config "$CONFIG" \
        --format "$FORMAT" || rc=$?

    case "$rc" in
        0)  ok "SQL formatting check passed cleanly."; return 0 ;;
        1)  err "SQL lint violations found in ${SQL_FILE}."; return 1 ;;
        *)  err "SQLFluff fatal error (exit ${rc}) — check config/syntax, not lint rules."; return 2 ;;
    esac
}

# --- Fix (atomic; --dry-run preview) ---------------------------------------
do_fix() {
    log "Fixing ${SQL_FILE} (dialect=${DIALECT})..."
    TMP_DIR=$(mktemp -d "${TMPDIR:-/tmp}/sqlfluff.XXXXXX")
    local tmp_file="$TMP_DIR/$(basename "$SQL_FILE")"
    cp -- "$SQL_FILE" "$tmp_file"

    local rc=0
    sqlfluff fix "$tmp_file" \
        --dialect "$DIALECT" \
        --config "$CONFIG" \
        --force || rc=$?

    case "$rc" in
        0) ;;  # sqlfluff fix returns 0 whether or not changes were made
        *)  die 2 "SQLFluff fatal error during fix (exit ${rc})" ;;
    esac

    if cmp -s "$SQL_FILE" "$tmp_file"; then
        info "no changes needed"
        return 0
    fi

    # Show what changed, even when applying. Useful in CI logs.
    if (( VERBOSE || DRY_RUN )); then
        log "--- diff (before → after) ---"
        diff -u --label "a/$SQL_FILE" --label "b/$SQL_FILE" \
            "$SQL_FILE" "$tmp_file" \
            | sed 's/^/    /' || true
        log "--- end diff ---"
    fi

    if (( DRY_RUN )); then
        info "[dry-run] would replace ${SQL_FILE} with fixed copy"
        return 0
    fi

    # Atomic replace. Permissions carry over because we copy, not move.
    mv -- "$tmp_file" "$SQL_FILE"
    info "rewrote ${SQL_FILE}"
    return 0
}

# --- Main ------------------------------------------------------------------
preflight
if (( FIX )); then
    do_fix
else
    do_lint
fi
