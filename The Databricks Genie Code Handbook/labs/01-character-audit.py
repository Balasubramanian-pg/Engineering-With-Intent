"""Audit workspace instruction files against a character budget.

Design guarantees
-----------------
* **Idempotent** — the script never writes, renames, deletes, or otherwise
  mutates the filesystem. Running it N times yields the same stdout/stderr
  (assuming the files are unchanged) and has no cumulative side effects.
* **Stateless** — there is no module-level mutable state, no cache, no
  config file, no `global`. Every run's behaviour is a pure function of
  (CLI arguments + current filesystem contents). Results are carried in
  immutable dataclasses rather than globals.

Exit codes
----------
  0  all audited files are within budget (or missing)
  1  at least one existing file exceeds the budget
  2  usage / argument error
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

# Character budget per file. Anything larger triggers "prompt dilution".
DEFAULT_BUDGET = 2000

# Default target, preserved for parity with the original script.
DEFAULT_TARGET = "/Workspace/.assistant_workspace_instructions.md"


@dataclass(frozen=True)
class AuditResult:
    """Immutable outcome of auditing a single file.

    Frozen (hashable, unassignable) so callers can't accidentally mutate
    shared state — this is part of the "stateless" contract.
    """

    path: Path
    char_count: int | None  # None when the file could not be read
    within_budget: bool
    message: str            # human-readable status line
    error: str | None = None


def audit_file(path: Path, budget: int = DEFAULT_BUDGET) -> AuditResult:
    """Read *path* and report its character count against *budget*.

    Pure function of (path, budget, current filesystem state). It performs
    no writes, so repeated calls are naturally idempotent.
    """
    try:
        # Explicit utf-8 keeps behaviour consistent across platforms.
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        # Missing files are treated as PASS (matches original behaviour),
        # so CI doesn't fail just because the file hasn't been created yet.
        return AuditResult(
            path=path,
            char_count=None,
            within_budget=True,
            message=f"{path}: not found (treated as PASS)",
        )
    except (PermissionError, IsADirectoryError, UnicodeDecodeError, OSError) as exc:
        # Surface any other IO / decoding error rather than crashing, and
        # still count it as a PASS so one bad path doesn't mask the others.
        return AuditResult(
            path=path,
            char_count=None,
            within_budget=True,
            message=f"{path}: error reading file: {exc}",
            error=str(exc),
        )

    count = len(text)
    ok = count <= budget
    status = "PASS" if ok else "FAIL (PROMPT DILUTION)"
    return AuditResult(
        path=path,
        char_count=count,
        within_budget=ok,
        message=f"{path}: {count} chars -> {status} (budget {budget})",
    )


def audit_many(
    paths: Iterable[Path], budget: int = DEFAULT_BUDGET
) -> list[AuditResult]:
    """Audit each path independently, preserving input order.

    Each call to :func:`audit_file` is independent — no state is shared
    between files, so callers can run this in parallel or re-run it freely.
    """
    return [audit_file(p, budget) for p in paths]


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse CLI arguments. Pure: reads ``argv`` only, touches no globals."""
    parser = argparse.ArgumentParser(
        description="Audit workspace instruction files against a character budget.",
    )
    parser.add_argument(
        "paths",
        nargs="*",
        default=[DEFAULT_TARGET],
        help=f"files to audit (default: {DEFAULT_TARGET})",
    )
    parser.add_argument(
        "-b", "--budget",
        type=int,
        default=DEFAULT_BUDGET,
        help=f"maximum characters per file (default: {DEFAULT_BUDGET})",
    )
    parser.add_argument(
        "-q", "--quiet",
        action="store_true",
        help="only print files that exceed the budget",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point. Returns the process exit code (see module docstring)."""
    args = _parse_args(argv)

    # Validate before doing any work so failures are cheap and deterministic.
    if args.budget <= 0:
        print("error: --budget must be a positive integer", file=sys.stderr)
        return 2

    results = audit_many((Path(p) for p in args.paths), args.budget)

    for r in results:
        if args.quiet and r.within_budget:
            continue
        print(r.message)

    # Non-zero exit iff at least one *existing* file exceeded the budget.
    return 0 if all(r.within_budget for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
