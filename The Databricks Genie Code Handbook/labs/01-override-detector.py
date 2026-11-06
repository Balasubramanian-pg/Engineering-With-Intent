"""Scan Databricks instruction files for contradictory directives.

Each rule line (anything that isn't blank or a comment) is normalized and
tested against a small registry of *conflict rules*. A conflict is reported
when every trigger label in a rule has at least one matching line.

Guarantees
----------
* **Idempotent** — :func:`scan_text` and :func:`scan_contradictions` are
  pure functions of their inputs (plus, for the latter, the current file
  contents). They write nothing and cache nothing, so re-running with
  unchanged inputs yields identical results.
* **Stateless** — no module-level mutable state, no globals, no caches.
  Results are frozen ``Conflict`` dataclasses; rule definitions live in an
  immutable tuple. Callers can freely re-invoke, memoize, or parallelize.

Scope / limitations
-------------------
This is a **lexical heuristic**, not a semantic analysis:

* It matches substrings after lowercasing and collapsing non-alphanumerics
  to spaces, so ``ANSI-SQL``, ``ansi  sql``, and ``ANSI SQL`` all match.
* It does **not** understand negation — ``"Do NOT use ANSI SQL"`` still
  matches the ``ANSI SQL`` trigger. Flagging such lines is left to the
  reader (or a future NLP pass); the ``matched_lines`` field surfaces the
  exact source lines so a human can judge.
* A missing file is treated as *"no conflicts"* (useful for CI), not an
  error. Read errors are reported on stderr and also yield no conflicts,
  so one bad path can't abort a batch.

Exit codes (when run as a script)
---------------------------------
  0  self-checks passed
  1  self-checks failed
"""

from __future__ import annotations

import re
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence


# --- Immutable result & rule types -----------------------------------------

@dataclass(frozen=True)
class Conflict:
    """A single detected contradiction.

    Frozen so results can't be mutated after the fact — callers can safely
    compare, hash, serialize, or share them without side effects.
    """

    kind: str
    description: str
    matched_lines: tuple[str, ...]  # source lines that satisfied each trigger


@dataclass(frozen=True)
class ConflictRule:
    """Declarative conflict definition.

    ``triggers`` is a tuple of ``(label, needles)`` pairs. The rule fires
    when **every** label has at least one line containing any of its
    needles (already normalized to lowercase space-separated tokens).
    """

    kind: str
    description: str
    triggers: tuple[tuple[str, tuple[str, ...]], ...]


# Registry. Immutable tuple → no runtime mutation, easy to extend by
# appending a new ConflictRule here (or passing one via ``rules=``).
CONFLICT_RULES: tuple[ConflictRule, ...] = (
    ConflictRule(
        kind="Language Conflict",
        description="Both ANSI SQL and PySpark mandated as default",
        triggers=(
            ("ANSI SQL", ("ansi sql",)),
            ("PySpark", ("pyspark",)),
        ),
    ),
)


# --- Pure helpers -----------------------------------------------------------

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def _normalize(text: str) -> str:
    """Lowercase and collapse non-alphanumerics to spaces.

    ``"ANSI-SQL"`` → ``"ansi sql"``; ``"  PySpark  "`` → ``"pyspark"``.
    Makes substring matching robust to hyphens, extra whitespace, and case.
    """
    return " ".join(_NON_ALNUM.sub(" ", text.lower()).split())


def iter_rule_lines(content: str) -> Iterable[str]:
    """Yield stripped, non-comment, non-blank lines from *content*.

    Handles *indented* comments (``"    # note"``) correctly — the original
    checked ``startswith('#')`` against the raw line, so indented comments
    leaked through as rules. Pure generator; no state.
    """
    for raw in content.splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        yield stripped


def scan_text(
    content: str,
    rules: Sequence[ConflictRule] = CONFLICT_RULES,
) -> list[Conflict]:
    """Return all conflicts found in *content*. Pure and idempotent.

    Parameters
    ----------
    content : str
        Raw text of an instruction file.
    rules : Sequence[ConflictRule]
        Rule registry to test. Defaults to :data:`CONFLICT_RULES`.

    Returns
    -------
    list[Conflict]
        Empty list means "clean". Ordering follows ``rules`` order.
    """
    lines = list(iter_rule_lines(content))
    normalized = [_normalize(line) for line in lines]

    conflicts: list[Conflict] = []
    for rule in rules:
        per_trigger_hits: list[list[str]] = []
        for _label, needles in rule.triggers:
            hits = [
                orig
                for orig, norm in zip(lines, normalized)
                if any(needle in norm for needle in needles)
            ]
            per_trigger_hits.append(hits)

        # Rule fires only when *every* trigger matched at least one line.
        if all(per_trigger_hits):
            # Flatten, dedupe while preserving order (dict preserves insert).
            seen: dict[str, None] = {}
            for hits in per_trigger_hits:
                for h in hits:
                    seen.setdefault(h, None)
            conflicts.append(
                Conflict(
                    kind=rule.kind,
                    description=rule.description,
                    matched_lines=tuple(seen.keys()),
                )
            )

    return conflicts


def scan_contradictions(
    filepath: str | Path,
    rules: Sequence[ConflictRule] = CONFLICT_RULES,
) -> list[Conflict]:
    """Read *filepath* and return its conflicts.

    A missing file yields ``[]`` (treated as "clean") so CI runs don't fail
    just because an instruction file hasn't been created yet — consistent
    with ``01-character-audit.py``. Read errors are logged to stderr and
    also produce ``[]``; a single bad path can't sink the batch.

    Performs no writes, so calling this repeatedly on unchanged files
    returns identical results (idempotent).
    """
    path = Path(filepath)
    try:
        content = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except (OSError, UnicodeDecodeError) as exc:
        print(f"warning: {path}: {exc}", file=sys.stderr)
        return []
    return scan_text(content, rules)


# --- Self-testing demo ------------------------------------------------------

def _check(condition: bool, message: str) -> bool:
    """Assertion helper that survives ``python -O``.

    The stdlib ``assert`` is stripped by ``-O``, which silently disables
    verification. This keeps the demo honest in CI.
    """
    if not condition:
        print(f"FAILED: {message}", file=sys.stderr)
        return False
    return True


def _demo() -> int:
    """Run the original scenario plus regression checks.

    Deterministic inputs => idempotent by construction. Returns a process
    exit code so this can gate CI.
    """
    print("Conflict detector initialized.")
    ok = True

    # --- 1. clean file → no conflicts ---------------------------------------
    ok &= _check(
        scan_text("Enforce ANSI SQL.\n") == [],
        "ANSI-SQL-only file must report no conflicts",
    )

    # --- 2. real conflict (mixed casing) ------------------------------------
    conflicting = "Use ANSI SQL only.\nPrefer PySpark for transforms.\n"
    result = scan_text(conflicting)
    ok &= _check(
        len(result) == 1 and result[0].kind == "Language Conflict",
        "conflicting file must report Language Conflict",
    )
    if result:
        ok &= _check(
            set(result[0].matched_lines)
            == {"Use ANSI SQL only.", "Prefer PySpark for transforms."},
            "conflict must record both triggering source lines",
        )

    # --- 3. REGRESSION: indented comment is not a rule ----------------------
    ok &= _check(
        scan_text("    # ANSI SQL is mandatory\nUse PySpark.\n") == [],
        "indented comments must be ignored (original bug)",
    )

    # --- 4. hyphenated / whitespace variants still match --------------------
    ok &= _check(
        len(scan_text("Default is ANSI-SQL.\nAlso use PySpark.\n")) == 1,
        "'ANSI-SQL' should normalize to 'ansi sql' and match",
    )

    # --- 5. idempotency: two identical calls → equal results ----------------
    ok &= _check(
        scan_text(conflicting) == scan_text(conflicting),
        "repeated scans of identical input must be idempotent",
    )

    # --- 6. file-based path (uses a throwaway tempdir; no real writes) ------
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "instructions.md"
        p.write_text(conflicting, encoding="utf-8")
        ok &= _check(
            len(scan_contradictions(p)) == 1,
            "file-based scan should find the conflict",
        )
        ok &= _check(
            scan_contradictions(Path(td) / "missing.md") == [],
            "missing file must return [] rather than raising",
        )

    if ok:
        print("Conflict detector self-checks PASSED.")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(_demo())
